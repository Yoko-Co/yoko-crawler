"""Tests for job_manager.py."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from job_manager import (
    ConcurrencyLimitError,
    DomainAlreadyCrawlingError,
    Job,
    JobManager,
    JobNotFoundError,
    RESULTS_DIR,
    _humanize_error,
    _jobdir_for,
)


def make_fake_process(returncode=0, pid=12345):
    """Create a mock subprocess for testing."""
    proc = AsyncMock()
    proc.pid = pid
    proc.returncode = None

    async def fake_wait():
        proc.returncode = returncode
        return returncode

    proc.wait = fake_wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


class TestJob:
    def test_is_active(self):
        job = Job(job_id="abc", domain="example.com", status="queued")
        assert job.is_active

        job.status = "running"
        assert job.is_active

        job.status = "completed"
        assert not job.is_active

        job.status = "failed"
        assert not job.is_active

    def test_elapsed_seconds(self):
        job = Job(job_id="abc", domain="example.com", started_at=time.time() - 10)
        assert 9 <= job.elapsed_seconds() <= 11

    def test_file_paths(self):
        job = Job(job_id="abc123", domain="example.com")
        assert job.status_file.name == "abc123.status.json"
        assert job.result_file.name == "abc123.jsonl"
        assert job.log_file_path.name == "abc123.log"


def test_jobdir_for_neutralizes_path_traversal():
    # A hostile domain must never resolve outside JOBDIR_ROOT -- the path is rmtree'd.
    import job_manager as jm_mod

    root = jm_mod.JOBDIR_ROOT.resolve()
    for hostile in ("..", ".", "...", "../../etc", ".hidden", "a/../b"):
        p = jm_mod._jobdir_for(hostile)
        assert p.parent == root  # stays directly under the root, never escapes


@pytest.fixture(autouse=True)
def use_tmp_results_dir(tmp_path, monkeypatch):
    """Use temp directories for RESULTS_DIR + JOBDIR_ROOT in all job manager tests."""
    monkeypatch.setattr("job_manager.RESULTS_DIR", tmp_path)
    monkeypatch.setattr("job_manager.JOBDIR_ROOT", tmp_path / "jobdirs")
    yield


class TestJobManager:
    async def test_start_job(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com")

        assert job.domain == "example.com"
        assert job.status == "running"
        assert len(job.job_id) == 16
        assert job.delay == 1.0

    async def test_delay_passed_to_subprocess(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job("example.com", delay=3.0)

        assert job.delay == 3.0
        args = mock_exec.call_args.args
        assert args[args.index("--delay") + 1] == "3.0"

    async def test_resumable_passes_jobdir_to_subprocess(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()
        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job("example.com", resumable=True)
        assert job.resumable is True
        args = mock_exec.call_args.args
        assert args[args.index("--jobdir") + 1] == str(job.jobdir)
        assert job.jobdir == _jobdir_for("example.com")

    async def test_non_resumable_omits_jobdir(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()
        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job("example.com")
        assert job.resumable is False
        assert job.jobdir is None
        assert "--jobdir" not in mock_exec.call_args.args

    async def test_reset_discards_existing_jobdir_before_start(self):
        jm = JobManager(max_concurrent=3)
        jd = _jobdir_for("example.com")
        jd.mkdir(parents=True, exist_ok=True)
        (jd / "requests.seen").write_text("stale-frontier")
        proc = make_fake_process()
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            await jm.start_job("example.com", resumable=True, reset=True)
        assert not (jd / "requests.seen").exists()  # prior resume state discarded

    async def _run_monitor_with_close_reason(self, jm, close_reason):
        proc = make_fake_process(returncode=0)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=True)
        # Simulate the spider having persisted a JOBDIR + written its close reason.
        job.jobdir.mkdir(parents=True, exist_ok=True)
        (job.jobdir / "requests.seen").write_text("frontier")
        job.status_file.write_text(
            json.dumps({"status": "completed", "close_reason": close_reason})
        )
        await jm._monitor(job.job_id)
        return job

    async def test_monitor_deletes_jobdir_when_finished(self):
        jm = JobManager(max_concurrent=3)
        job = await self._run_monitor_with_close_reason(jm, "finished")
        # Frontier drained -> resume state dropped so the next crawl is fresh.
        assert not job.jobdir.exists()

    async def test_monitor_keeps_jobdir_when_paused(self):
        jm = JobManager(max_concurrent=3)
        job = await self._run_monitor_with_close_reason(jm, "closespider_timeout")
        # Paused at the session cap -> keep the JOBDIR so the next session resumes.
        assert (job.jobdir / "requests.seen").exists()

    async def test_jobdir_survives_a_spider_that_never_constructed(self):
        """#103. A spider that never opened usually never touched the frontier, so deleting it
        discards a good multi-session crawl over a failure that never came near it.

        The token does NOT prove the frontier is innocent -- a frontier Scrapy cannot READ
        errbacks the crawl Deferred and arrives as exactly this token
        (tests/test_corrupt_jobdir_reaches_errback.py pins that). What bounds the resulting
        retry loop is `strike_jobdir` on the run_spider side, covered by
        TestUnopenableJobdirSelfHeals; this test covers only the keep itself."""
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process(returncode=1)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=True)
        job.jobdir.mkdir(parents=True, exist_ok=True)
        (job.jobdir / "requests.seen").write_text("frontier")
        job.status_file.write_text(json.dumps(
            {"status": "failed", "failure_reason": "spider_init_error"}))
        await jm._monitor(job.job_id)
        assert (job.jobdir / "requests.seen").exists(), (
            "the frontier is untouched by a spider that never opened; the next session "
            "should resume it rather than re-crawl from scratch"
        )

    async def test_a_different_failure_still_drops_the_frontier(self):
        """NOT generalised to "failed". A corrupt JOBDIR is itself a cause of not-starting,
        so refusing to delete on any failure would make every retry fail identically and
        permanently brick the domain -- the exact hazard `reset_incompatible_jobdir` warns
        about."""
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process(returncode=1)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=True)
        job.jobdir.mkdir(parents=True, exist_ok=True)
        (job.jobdir / "requests.seen").write_text("frontier")
        job.status_file.write_text(json.dumps(
            {"status": "failed", "failure_reason": "crawl_error"}))
        await jm._monitor(job.job_id)
        assert not job.jobdir.exists(), "a half-written frontier must still be dropped"

    async def test_monitor_deletes_jobdir_on_nongraceful_close(self):
        # No close_reason (killed/OOM/crash before the spider flushed) -> the frontier
        # may be half-written, so drop it rather than resume a corrupt JOBDIR.
        jm = JobManager(max_concurrent=3)
        job = await self._run_monitor_with_close_reason(jm, None)
        assert not job.jobdir.exists()

    async def test_impersonate_passed_to_subprocess(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job("example.com", impersonate="chrome")

        assert job.impersonate == "chrome"
        args = mock_exec.call_args.args
        assert "--impersonate" in args
        assert args[args.index("--impersonate") + 1] == "chrome"

    async def test_impersonate_defaults_off(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            await jm.start_job("example.com")

        args = mock_exec.call_args.args
        assert args[args.index("--impersonate") + 1] == "off"

    async def test_user_agent_passed_on_argv(self):
        # UA override (not a secret) rides on argv.
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()
        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job("example.com", user_agent="Mozilla/5.0 X")
        assert job.user_agent == "Mozilla/5.0 X"
        args = mock_exec.call_args.args
        assert args[args.index("--user-agent") + 1] == "Mozilla/5.0 X"

    async def test_proxy_passed_via_env_not_argv(self):
        # issue #22: a proxy URL can carry credentials, so it is handed to the subprocess in
        # the environment (YOKO_CRAWL_PROXY), NOT on argv where `ps` would expose it.
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()
        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job("example.com", proxy="http://user:pass@box:8080")
        assert job.proxy == "http://user:pass@box:8080"
        # Never on argv (the whole point -- creds must not be world-readable via `ps`).
        assert "--proxy" not in mock_exec.call_args.args
        assert "http://user:pass@box:8080" not in mock_exec.call_args.args
        # Delivered in the child's environment instead.
        env = mock_exec.call_args.kwargs["env"]
        assert env["YOKO_CRAWL_PROXY"] == "http://user:pass@box:8080"

    async def test_no_proxy_flag_by_default(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()
        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            await jm.start_job("example.com")
        assert "--proxy" not in mock_exec.call_args.args
        # env=None -> the subprocess inherits the parent environment unchanged (no proxy).
        assert mock_exec.call_args.kwargs.get("env") is None

    async def test_no_user_agent_flag_by_default(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()
        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            await jm.start_job("example.com")
        args = mock_exec.call_args.args
        assert "--cookies" not in args and "--user-agent" not in args
        # No proxy -> env=None, so the subprocess inherits the parent environment unchanged.
        assert mock_exec.call_args.kwargs.get("env") is None

    async def test_profile_and_emit_content_passed_to_subprocess(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            job = await jm.start_job(
                "example.com", profile="presale", emit_content=True
            )

        assert job.profile == "presale"
        assert job.emit_content is True
        args = mock_exec.call_args.args
        assert args[args.index("--profile") + 1] == "presale"
        assert "--emit-content" in args

    async def test_emit_content_flag_omitted_by_default(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch(
            "job_manager.asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            await jm.start_job("example.com")

        args = mock_exec.call_args.args
        assert args[args.index("--profile") + 1] == "standard"
        assert "--emit-content" not in args

    async def test_invalid_profile_rejected(self):
        jm = JobManager(max_concurrent=3)
        with pytest.raises(ValueError):
            await jm.start_job("example.com", profile="aggressive")

    async def test_status_response_echoes_profile_and_emit_content(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job(
                "example.com", profile="presale", emit_content=True
            )

        response = await jm.get_status_response(job)
        assert response["profile"] == "presale"
        assert response["emit_content"] is True

    async def test_concurrency_limit(self):
        jm = JobManager(max_concurrent=1)
        proc = make_fake_process()

        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job1 = await jm.start_job("example1.com")
            assert job1.is_active

            with pytest.raises(ConcurrencyLimitError):
                await jm.start_job("example2.com")

    async def test_duplicate_domain(self):
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process()

        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job1 = await jm.start_job("example.com")
            assert job1.is_active

            with pytest.raises(DomainAlreadyCrawlingError):
                await jm.start_job("example.com")

    def test_get_job_not_found(self):
        jm = JobManager()
        with pytest.raises(JobNotFoundError):
            jm.get_job("nonexistent")

    async def test_delete_job(self):
        jm = JobManager()
        proc = make_fake_process()

        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com")
            job_id = job.job_id

        await jm.delete_job(job_id)
        with pytest.raises(JobNotFoundError):
            jm.get_job(job_id)

    async def test_delete_idempotent(self):
        jm = JobManager()
        await jm.delete_job("nonexistent")  # Should not raise.

    async def test_get_status_response(self):
        jm = JobManager()
        job = Job(
            job_id="abc123def456789a",
            domain="example.com",
            impersonate="chrome",
            delay=2.5,
            status="running",
            started_at=time.time() - 60,
        )
        jm._jobs["abc123def456789a"] = job

        response = await jm.get_status_response(job)
        assert response["job_id"] == "abc123def456789a"
        assert response["domain"] == "example.com"
        assert response["status"] == "running"
        assert response["impersonate"] == "chrome"
        assert response["delay"] == 2.5
        assert 59 <= response["elapsed_seconds"] <= 61
        # A running job has no classified failure yet.
        assert response["failure_reason"] is None

    async def test_get_status_response_propagates_failure_reason(self):
        # failure_reason written to status.json must reach the GET /crawl/{id} payload
        # (issue #44) -- the whole point of the field. Mirrors the close_reason contract.
        jm = JobManager()
        proc = make_fake_process(returncode=0)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("exmaple.com", resumable=True)
        job.status_file.write_text(
            json.dumps({
                "status": "failed",
                "failure_reason": "unreachable",
                "error": "target unreachable",
                "close_reason": "finished",
            })
        )
        await jm._monitor(job.job_id)
        response = await jm.get_status_response(job)
        assert response["status"] == "failed"
        assert response["failure_reason"] == "unreachable"

    async def test_get_status_response_propagates_blocking(self):
        # The `blocking` counts written to status.json must reach GET /crawl/{id} so the
        # corpus/frontend can surface the Cloudflare-challenge vs origin-403 split.
        jm = JobManager()
        proc = make_fake_process(returncode=0)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=True)
        job.status_file.write_text(
            json.dumps({
                "status": "running",
                "urls_crawled": 60,
                "blocking": {
                    "waf_challenge_count": 12,
                    "origin_forbidden_count": 8,
                    "status_counts": {"200": 40, "403": 20},
                },
            })
        )
        response = await jm.get_status_response(job)
        assert response["blocking"]["waf_challenge_count"] == 12
        assert response["blocking"]["origin_forbidden_count"] == 8
        assert response["blocking"]["status_counts"] == {"200": 40, "403": 20}

    async def test_get_status_response_blocking_defaults_when_absent(self):
        # An older status file (or mid-crawl before any 403) has no `blocking` -> a zeroed
        # shape, so a consumer always sees the same keys.
        jm = JobManager()
        job = Job(
            job_id="abc123def4560000",
            domain="example.com",
            status="running",
            started_at=time.time(),
        )
        jm._jobs[job.job_id] = job
        response = await jm.get_status_response(job)
        assert response["blocking"] == {
            "waf_challenge_count": 0,
            "origin_forbidden_count": 0,
            "status_counts": {},
        }

    def test_startup_sweep(self, tmp_path):
        """Startup sweep should delete old orphaned files."""
        # tmp_path is the same as the monkeypatched RESULTS_DIR from autouse fixture.
        # Create old orphaned files.
        old_file = tmp_path / "oldjob.jsonl"
        old_file.write_text("test")
        # Set mtime to 10 minutes ago.
        old_mtime = time.time() - 600
        os.utime(old_file, (old_mtime, old_mtime))

        # Create recent file (should not be deleted).
        new_file = tmp_path / "newjob.jsonl"
        new_file.write_text("test")

        jm = JobManager()
        jm.startup_sweep()

        assert not old_file.exists()
        assert new_file.exists()


class TestHumanizeError:
    def test_known_errors(self):
        assert "memory" in _humanize_error("memusage_exceeded").lower()
        assert "cancelled" in _humanize_error("cancel").lower()

    def test_unknown_error_passthrough(self):
        assert _humanize_error("some_weird_error") == "some_weird_error"

    def test_none_error(self):
        assert _humanize_error(None) == "Crawl failed"


class TestSpiderNeverStarted:
    """Issue #98: a subprocess that exits 0 having written NO status file did not fail --
    it fell through to `elif returncode == 0` and reported a COMPLETED zero-page crawl,
    which yoko-corpus cannot tell apart from a site that genuinely had nothing.

    Measured on the pre-fix tree with a spider raising in `__init__`: exit code 0, and the
    job reported `completed`. Note the discriminator is NOT a missing status file --
    `_write_initial_status` writes a `queued` stub before spawning, and that stub is exactly
    what satisfied every existing check. `ProgressWriter` attaches on `spider_opened` and
    writes every 3s after, so a crawl that actually ran has moved off `queued`."""

    async def _monitor_with(self, jm, *, returncode, status_payload):
        proc = make_fake_process(returncode=returncode)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=False)
        if status_payload is not None:
            job.status_file.write_text(json.dumps(status_payload))
        else:
            job.status_file.unlink(missing_ok=True)
        await jm._monitor(job.job_id)
        return job

    async def test_a_status_left_at_the_queued_stub_is_failed_not_completed(self):
        """THE bug. The spider never opened, so our own pre-spawn stub is still there --
        and it looked enough like a status to carry the crawl through to `completed`."""
        jm = JobManager(max_concurrent=3)
        job = await self._monitor_with(
            jm, returncode=0,
            status_payload={"status": "queued", "urls_crawled": 0},
        )
        assert job.status == "failed", (
            "a status still at the pre-spawn stub means the spider never opened; reporting "
            "it completed hands the corpus a zero-page crawl that looks like a real result"
        )
        assert "never started" in (job.error or "")

    async def test_clean_exit_with_no_status_file_is_failed_too(self):
        """Asserts the REASON, not just the status. Dropping the `status_data is None` branch
        makes `.get()` raise on None and the job lands on `failed` anyway -- so a status-only
        assertion passed against a broken guard (caught by mutation, #98)."""
        jm = JobManager(max_concurrent=3)
        job = await self._monitor_with(jm, returncode=0, status_payload=None)
        assert job.status == "failed"
        assert "never started" in (job.error or ""), (
            "must fail through the guard with its explanation, not incidentally via a crash"
        )

    async def test_a_real_completed_crawl_is_untouched(self):
        """The guard must not turn ordinary successful crawls red."""
        jm = JobManager(max_concurrent=3)
        job = await self._monitor_with(
            jm, returncode=0,
            status_payload={"status": "completed", "urls_crawled": 42},
        )
        assert job.status == "completed"

    async def test_a_crawl_that_wrote_progress_but_no_terminal_status_still_completes(self):
        """A status file exists but was never finalised (the monitor is the backstop for
        that case, and #98 must not change it): the spider DID open, so exit 0 still means
        completed. This is the boundary the new branch must not swallow."""
        jm = JobManager(max_concurrent=3)
        job = await self._monitor_with(
            jm, returncode=0,
            status_payload={"status": "running", "urls_crawled": 7},
        )
        assert job.status == "completed"

    async def test_a_crawl_that_wrote_ROWS_is_never_called_never_started(self):
        """THE false positive, reproduced by review end-to-end: `ProgressWriter._write_status`
        swallows OSError by design, so a read-only or full status path lets a crawl fetch
        thousands of pages, write every feed row, exit 0 -- and leave the status at the stub.
        Calling that "never started" fails a client's crawl that WORKED and hides a real
        inventory behind a 409. Worse per occurrence than the empty success it replaces.

        The feed is the corroborating evidence: a spider that never constructed cannot have
        emitted a row."""
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process(returncode=0)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=False)
        job.status_file.write_text(json.dumps({"status": "queued", "urls_crawled": 0}))
        job.result_file.write_text('{"url": "https://example.com/", "status": 200}\n')
        await jm._monitor(job.job_id)
        assert job.status == "completed", (
            "rows on disk prove the spider opened, whatever the status file says"
        )

    async def test_an_empty_output_file_is_not_evidence_of_a_crawl(self):
        """The boundary: the feed being CREATED is not the signal -- Scrapy opens it early.
        Only content proves a row was emitted."""
        jm = JobManager(max_concurrent=3)
        proc = make_fake_process(returncode=0)
        with patch("job_manager.asyncio.create_subprocess_exec", return_value=proc):
            job = await jm.start_job("example.com", resumable=False)
        job.status_file.write_text(json.dumps({"status": "queued"}))
        job.result_file.write_text("")
        await jm._monitor(job.job_id)
        assert job.status == "failed"

    async def test_a_nonzero_exit_keeps_its_own_message(self):
        jm = JobManager(max_concurrent=3)
        job = await self._monitor_with(jm, returncode=3, status_payload=None)
        assert job.status == "failed"
        assert "exited with code 3" in (job.error or "")
