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
