"""
Job lifecycle management for crawl subprocesses.

Handles subprocess spawning, monitoring, concurrency control,
periodic cleanup, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles
import structlog

logger = structlog.get_logger()

RESULTS_DIR = Path(os.environ.get("YOKO_CRAWL_RESULTS_DIR", "/opt/yoko-crawl/results"))

# Persistent per-domain Scrapy JOBDIRs for resumable crawls (Phase C). Kept SEPARATE
# from RESULTS_DIR (which is TTL-cleaned) because a JOBDIR must survive between the
# sessions of one crawl. Scrapy persists the request frontier + dupefilter here, so a
# re-launch with the same JOBDIR resumes instead of re-fetching from the seed.
JOBDIR_ROOT = Path(os.environ.get("YOKO_CRAWL_JOBDIR", "/opt/yoko-crawl/jobdirs"))


def _jobdir_for(domain: str) -> Path:
    """The stable per-domain JOBDIR path, GUARANTEED to sit directly under JOBDIR_ROOT
    -- this path is handed to shutil.rmtree, so it must never escape. The char-class
    filter alone is NOT enough: dots are in the allow-set, so `.`, `..`, and
    leading/trailing-dot values would resolve to JOBDIR_ROOT itself or its PARENT (and
    `reset` would rmtree it). Strip dot-only components to a single safe filename, then
    assert containment as defense-in-depth (raises rather than deleting the wrong dir)."""
    safe = re.sub(r"[^a-z0-9.-]", "_", domain.lower()).strip(".") or "_"
    path = (JOBDIR_ROOT / safe).resolve()
    if path.parent != JOBDIR_ROOT.resolve():
        raise ValueError(f"refusing unsafe jobdir path for domain {domain!r}")
    return path

# Valid crawl profiles. The HTTP layer also constrains this via a Literal, but
# start_job guards it too so a direct programmatic caller can't forward an
# unknown value to the subprocess (where it would fail late as a job error).
VALID_PROFILES = ("standard", "presale")

# Watchdog timeout: CLOSESPIDER_TIMEOUT (7200s) + 5min buffer.
_WATCHDOG_TIMEOUT = 7500

# How long completed/failed jobs are retained before cleanup.
_JOB_TTL_SECONDS = 3600  # 1 hour

# Cleanup sweep interval.
_CLEANUP_INTERVAL = 300  # 5 minutes

# How long to wait for process exit on DELETE before SIGKILL.
_DELETE_KILL_TIMEOUT = 5


@dataclass
class Job:
    """Represents a crawl job and its associated state."""

    job_id: str
    domain: str
    impersonate: str = "off"
    delay: float = 1.0
    profile: str = "standard"
    emit_content: bool = False
    # Resumable crawl (Phase C): when true the spider runs with a persistent
    # per-domain JOBDIR, so a crawl that pauses (session cap) resumes on the next run.
    resumable: bool = False
    # User-Agent override sent with every request (e.g. a specific browser build).
    user_agent: str | None = None
    # Forward-proxy URL for a bot-block retry (issue #22); None for a normal crawl.
    proxy: str | None = None
    started_at: float = field(default_factory=time.time)
    status: str = "queued"  # queued, running, completed, failed
    error: str | None = None
    completed_at: float | None = None
    failed_at: float | None = None
    process: asyncio.subprocess.Process | None = None
    monitor_task: asyncio.Task | None = None
    active_readers: int = 0

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def status_file(self) -> Path:
        return RESULTS_DIR / f"{self.job_id}.status.json"

    @property
    def result_file(self) -> Path:
        return RESULTS_DIR / f"{self.job_id}.jsonl"

    @property
    def jobdir(self) -> Path | None:
        """The persistent per-domain JOBDIR for a resumable crawl, else None. Keyed on
        domain (not job_id) so consecutive sessions of one crawl share resume state."""
        return _jobdir_for(self.domain) if self.resumable else None

    @property
    def log_file_path(self) -> Path:
        return RESULTS_DIR / f"{self.job_id}.log"

    def elapsed_seconds(self) -> int:
        if self.completed_at:
            return int(self.completed_at - self.started_at)
        if self.failed_at:
            return int(self.failed_at - self.started_at)
        return int(time.time() - self.started_at)

    def cleanup_files(self) -> None:
        """Remove all files associated with this job (status/result/log).

        Deliberately does NOT touch the JOBDIR: it is keyed on DOMAIN, not job_id, and
        must OUTLIVE this session's job record so the next session (a new job) can
        resume it. The JOBDIR's own lifecycle is driven by the crawl outcome in
        `_monitor` (delete on 'finished' or a non-graceful kill) and by `reset`; a
        stale/abandoned JOBDIR is a garbage-collection concern tracked separately."""
        for path in (self.status_file, self.result_file, self.log_file_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        # Also clean up temp files from atomic writes.
        tmp = Path(str(self.status_file) + ".tmp")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class ConcurrencyLimitError(Exception):
    """Raised when the max concurrent crawl limit is reached."""


class DomainAlreadyCrawlingError(Exception):
    """Raised when the requested domain is already being crawled."""


class JobNotFoundError(Exception):
    """Raised when a job ID is not found."""


class JobManager:
    """Manages crawl job lifecycle with subprocess isolation."""

    def __init__(self, max_concurrent: int = 3):
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._max_concurrent = max_concurrent

    def get_job(self, job_id: str) -> Job:
        """Look up a job by ID."""
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        return job

    @property
    def active_job_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.is_active)

    async def start_job(
        self,
        domain: str,
        impersonate: str = "off",
        delay: float = 1.0,
        profile: str = "standard",
        emit_content: bool = False,
        resumable: bool = False,
        reset: bool = False,
        user_agent: str | None = None,
        proxy: str | None = None,
    ) -> Job:
        """
        Start a new crawl job for the given domain.

        Acquires the lock for the entire check-and-spawn sequence to prevent
        race conditions between concurrent POST requests.

        ``impersonate`` selects a browser TLS fingerprint (off/chrome/firefox/
        safari/random) for sites behind TLS-fingerprinting WAFs. ``delay`` is the
        minimum seconds between requests (its companion knob for aggressive WAFs).
        ``profile`` ("standard"/"presale") selects the politeness bundle.
        ``emit_content`` includes each page's main-content text in the output.
        ``resumable`` runs the spider with a persistent per-domain JOBDIR, so a crawl
        that pauses at the session cap RESUMES on the next run instead of re-fetching
        from the seed (Phase C). ``reset`` discards any existing JOBDIR first, forcing
        a fresh scan (e.g. a "request fresh crawl" that must re-detect changes).
        ``user_agent`` overrides the User-Agent sent on every request. ``proxy`` routes
        every request through a forward proxy (the trusted-IP egress, issue #22).
        """
        if profile not in VALID_PROFILES:
            raise ValueError(f"invalid profile: {profile!r}")

        async with self._lock:
            # Check concurrency limit.
            active = sum(1 for j in self._jobs.values() if j.is_active)
            if active >= self._max_concurrent:
                raise ConcurrencyLimitError()

            # Check duplicate domain.
            for j in self._jobs.values():
                if j.domain == domain and j.is_active:
                    raise DomainAlreadyCrawlingError()

            # A fresh scan discards prior resume state (frontier + dupefilter) so the
            # crawl starts from the seed and re-detects changes. Only meaningful with
            # a JOBDIR (resumable); harmless otherwise.
            if reset:
                shutil.rmtree(_jobdir_for(domain), ignore_errors=True)

            # Generate unique job ID with collision check.
            job_id = secrets.token_hex(8)
            while job_id in self._jobs:
                job_id = secrets.token_hex(8)

            job = Job(
                job_id=job_id,
                domain=domain,
                impersonate=impersonate,
                delay=delay,
                profile=profile,
                emit_content=emit_content,
                resumable=resumable,
                user_agent=user_agent,
                proxy=proxy,
            )
            self._jobs[job_id] = job

        # Write initial status file so GET never hits FileNotFoundError.
        self._write_initial_status(job)

        # Spawn subprocess outside the lock to minimize lock hold time.
        try:
            await self._spawn_subprocess(job)
        except Exception:
            logger.exception("Failed to spawn subprocess", job_id=job_id)
            job.status = "failed"
            job.error = "Failed to start crawl subprocess"
            job.failed_at = time.time()
            return job

        # Transition to running now that subprocess is alive.
        job.status = "running"

        # Start monitor task.
        job.monitor_task = asyncio.create_task(self._monitor(job_id))

        return job

    def _write_initial_status(self, job: Job) -> None:
        """Write initial queued status file before subprocess starts."""
        data = {
            "status": "queued",
            "urls_discovered": 0,
            "urls_crawled": 0,
            "updated_at": time.time(),
            "error": None,
        }
        try:
            with open(job.status_file, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    async def _spawn_subprocess(self, job: Job) -> None:
        """Spawn the Scrapy subprocess for a job."""
        log_fh = open(job.log_file_path, "w")

        cmd = [
            sys.executable,
            "run_spider.py",
            "--domain",
            job.domain,
            "--output",
            str(job.result_file),
            "--status-file",
            str(job.status_file),
            "--impersonate",
            job.impersonate,
            "--delay",
            str(job.delay),
            "--profile",
            job.profile,
        ]
        # --emit-content is a store_true flag: pass it only when enabled.
        if job.emit_content:
            cmd.append("--emit-content")

        # Resumable crawl: Scrapy persists the frontier + dupefilter to this dir and
        # resumes from it on the next run for the same domain.
        if job.jobdir is not None:
            job.jobdir.parent.mkdir(parents=True, exist_ok=True)
            cmd += ["--jobdir", str(job.jobdir)]

        # UA override (not a secret) rides on argv; passed only when set.
        if job.user_agent:
            cmd += ["--user-agent", job.user_agent]

        # The proxy URL can embed credentials (user:pass@), so it must NOT ride on argv --
        # argv is world-readable via `ps` / /proc/<pid>/cmdline. Hand it to the child in the
        # environment instead; run_spider reads YOKO_CRAWL_PROXY and validates + SSRF-vets it
        # before use (issue #22). env=None inherits the parent environment unchanged.
        proc_env = {**os.environ, "YOKO_CRAWL_PROXY": job.proxy} if job.proxy else None

        try:
            job.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log_fh,
                cwd=str(Path(__file__).parent),
                env=proc_env,
            )
        except Exception:
            log_fh.close()
            raise
        else:
            # Parent no longer needs the fd; child inherited it.
            log_fh.close()

    async def _monitor(self, job_id: str) -> None:
        """
        Monitor a subprocess until completion or timeout.

        Wrapped in try/except to prevent zombie jobs on unhandled errors.
        Reads the status file after process exit to determine the actual
        outcome (Scrapy exits 0 for all close reasons including memusage_exceeded).
        """
        try:
            job = self._jobs.get(job_id)
            if not job or not job.process:
                return

            try:
                await asyncio.wait_for(
                    job.process.wait(), timeout=_WATCHDOG_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Watchdog timeout, terminating subprocess",
                    job_id=job_id,
                )
                await self._kill_process(job.process)
                if job_id not in self._jobs:
                    return
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = "Crawl exceeded maximum duration"
                job.failed_at = time.time()
                # Hard-killed mid-write -> the JOBDIR frontier may be half-written and
                # must not be resumed. Drop it (re-crawl fresh; ingested pages persist).
                if job.resumable and job.jobdir is not None:
                    shutil.rmtree(job.jobdir, ignore_errors=True)
                return

            # Process exited — check if job was deleted during wait.
            if job_id not in self._jobs:
                return

            job = self._jobs[job_id]

            # Read status file for authoritative final status.
            status_data = await self._read_status_file(job)
            if status_data and status_data.get("status") in ("completed", "failed"):
                job.status = status_data["status"]
                job.error = status_data.get("error")
            elif job.process.returncode == 0 and not self._spider_never_opened(job, status_data):
                job.status = "completed"
            elif job.process.returncode == 0:
                # Exit 0 with the status file STILL the queued stub is not success -- it is a
                # crawl that never opened (#98).
                #
                # The discriminator is deliberately "the spider never overwrote our stub",
                # not "there is no status file": `_write_initial_status` always writes one
                # before the subprocess starts, so absence never happens on this path. That
                # is exactly how the bug survived -- the stub satisfied every existing check,
                # the `("completed", "failed")` branch was missed, and the crawl fell through
                # to `elif returncode == 0` and reported COMPLETED with zero pages, which
                # yoko-corpus cannot distinguish from a site that genuinely had nothing.
                #
                # `run_spider` now catches spider-construction failures itself and exits
                # non-zero, so that route lands on the `else` below, not here.
                #
                # An earlier version of this comment justified the second guard with a list of
                # causes -- kill, OOM, a failed exec -- that was simply WRONG: every one of
                # them yields a NON-ZERO returncode and never reaches this branch (#98 review).
                # The real independent cause is narrower and worth stating accurately: a
                # SIGTERM landing inside the startup window leaves the crawl Deferred unfired,
                # so guard 1 never runs and the process still exits 0. Beyond that this is a
                # regression guard -- it holds if a future edit drops the errback -- and it
                # cannot be bypassed from inside the subprocess. That is a smaller claim than
                # the one it replaces, and it is the true one.
                job.status = "failed"
                job.error = (
                    "Crawl never started: the process exited cleanly, produced no output, "
                    "and no status was ever reported for it, so nothing was crawled. "
                    "Reported as failed rather than an empty success."
                )
                logger.error(
                    "CRAWL NEVER STARTED: the subprocess exited cleanly but produced no "
                    "output and never reported a status, so the spider never opened. "
                    "Reported as failed rather than an empty success, which the corpus "
                    "cannot tell apart from a site that genuinely had nothing. Check the "
                    "job log for a startup traceback.",
                    job_id=job_id,
                )
            else:
                job.status = "failed"
                job.error = f"Process exited with code {job.process.returncode}"

            # Resumable JOBDIR lifecycle:
            #  - "finished" (frontier drained -> whole site crawled): delete it, so the
            #    next crawl of this domain starts fresh and re-detects changes.
            #  - a GRACEFUL pause (closespider_timeout/itemcount -- close_reason present):
            #    keep it, so the next session resumes the frontier.
            #  - NO graceful close (close_reason absent: killed / OOM / crash before the
            #    spider could flush its disk queue): delete it. A half-written frontier
            #    must not be resumed (corrupt); re-crawl fresh. The pages ingested so
            #    far already persist in the corpus, so no fetched work is lost.
            if job.resumable and job.jobdir is not None:
                close_reason = status_data.get("close_reason") if status_data else None
                if close_reason == "finished" or close_reason is None:
                    shutil.rmtree(job.jobdir, ignore_errors=True)

            now = time.time()
            if job.status == "completed":
                job.completed_at = now
            else:
                job.failed_at = now

            logger.info(
                "Crawl finished",
                job_id=job_id,
                status=job.status,
                returncode=job.process.returncode,
            )

        except Exception:
            logger.exception("Monitor task failed", job_id=job_id)
            job = self._jobs.get(job_id)
            if job:
                job.status = "failed"
                job.error = "Internal monitor failure"
                job.failed_at = time.time()
                if job.process and job.process.returncode is None:
                    try:
                        job.process.kill()
                    except ProcessLookupError:
                        pass

    @staticmethod
    def _spider_never_opened(job: Job, status_data) -> bool:
        """True when the subprocess exited without the spider ever opening (#98).

        `ProgressWriter` attaches on `spider_opened` and writes every 3s after, so any crawl
        that ran has moved the status off the pre-spawn `queued` stub. A missing or unreadable
        file says the same thing, since `_write_initial_status` wrote one before spawning.

        But the status alone is NOT sufficient evidence, and this is the review finding that
        matters most here: `ProgressWriter._write_status` swallows OSError by design, so a
        read-only or full status path makes a crawl that fetched five thousand pages exit 0
        with the stub untouched -- and calling that "never started" fails a client's crawl that
        worked, hiding a real inventory behind a 409. That false positive is worse per
        occurrence than the empty-success it replaces.

        So the claim is corroborated against the FEED, which is written on a different path and
        cannot lie in the dangerous direction: a spider that never constructed cannot have
        emitted a row. Output present => the spider opened, whatever the status file says."""
        opened_by_status = not (
            status_data is None or status_data.get("status") == "queued"
        )
        if opened_by_status:
            return False
        try:
            produced_output = job.result_file.stat().st_size > 0
        except OSError:
            produced_output = False
        return not produced_output

    async def _read_status_file(self, job: Job) -> dict | None:
        """Read the status file asynchronously, returning None on any error."""
        try:
            async with aiofiles.open(job.status_file, "r") as f:
                content = await f.read()
            return json.loads(content)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    async def delete_job(self, job_id: str) -> None:
        """
        Delete a job: terminate process, wait for exit, clean up files.

        Waits for the process to exit before deleting files to prevent
        the ProgressWriter from recreating the status file.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return  # Idempotent

        # Terminate subprocess if running.
        if job.process and job.process.returncode is None:
            await self._kill_process(job.process, timeout=_DELETE_KILL_TIMEOUT)

        # Cancel monitor task.
        if job.monitor_task and not job.monitor_task.done():
            job.monitor_task.cancel()
            try:
                await job.monitor_task
            except (asyncio.CancelledError, Exception):
                pass

        # Clean up files after process is dead.
        job.cleanup_files()

        # Remove from dict.
        self._jobs.pop(job_id, None)

    async def _kill_process(
        self, process: asyncio.subprocess.Process, timeout: float = 10
    ) -> None:
        """Send SIGTERM, wait, then SIGKILL if needed."""
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def periodic_cleanup(self) -> None:
        """
        Periodically clean up completed/failed jobs older than 1 hour.

        Skips jobs with active readers to prevent cleanup-during-streaming race.
        Also cleans up stale temp files.
        """
        while True:
            try:
                await asyncio.sleep(_CLEANUP_INTERVAL)
                now = time.time()
                to_remove = []

                for job_id, job in self._jobs.items():
                    if job.is_active:
                        continue
                    if job.active_readers > 0:
                        continue
                    finished_at = job.completed_at or job.failed_at or 0
                    if now - finished_at > _JOB_TTL_SECONDS:
                        to_remove.append(job_id)

                for job_id in to_remove:
                    job = self._jobs.pop(job_id, None)
                    if job:
                        job.cleanup_files()
                        logger.info("Cleaned up expired job", job_id=job_id)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in periodic cleanup")

    def startup_sweep(self) -> None:
        """
        Synchronous sweep of orphaned files from previous runs.

        Runs before the app accepts requests (in lifespan, before yield).
        Only deletes files older than 5 minutes to avoid racing with
        concurrent container starts.
        """
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(RESULTS_DIR), 0o700)
        except OSError:
            pass  # May fail on mounted volumes with fixed permissions.

        cutoff = time.time() - 300  # 5 minutes
        patterns = ["*.jsonl", "*.status.json", "*.status.json.tmp", "*.log"]
        for pattern in patterns:
            for filepath in RESULTS_DIR.glob(pattern):
                try:
                    if filepath.stat().st_mtime < cutoff:
                        filepath.unlink()
                        logger.info("Cleaned orphaned file", path=str(filepath))
                except OSError:
                    pass

    async def shutdown_all(self) -> None:
        """Terminate all running subprocesses for graceful shutdown."""
        for job in list(self._jobs.values()):
            if job.process and job.process.returncode is None:
                try:
                    await self._kill_process(job.process, timeout=10)
                except Exception:
                    logger.exception(
                        "Error terminating subprocess", job_id=job.job_id
                    )

    async def get_status_response(self, job: Job) -> dict:
        """
        Build the full status response by merging in-memory state
        (authoritative for lifecycle) with status file (authoritative
        for progress counters).
        """
        # Read progress counters from status file.
        status_data = await self._read_status_file(job) or {}

        response = {
            "job_id": job.job_id,
            "status": job.status,  # In-memory is authoritative for lifecycle.
            "domain": job.domain,
            "impersonate": job.impersonate,
            "delay": job.delay,
            "profile": job.profile,
            "emit_content": job.emit_content,
            "resumable": job.resumable,
            "urls_discovered": status_data.get("urls_discovered", 0),
            "urls_crawled": status_data.get("urls_crawled", 0),
            # Scrapy close reason (None until the crawl closes). Lets a consumer
            # distinguish a natural `finished` from a safety-valve stop
            # (`closespider_timeout`/`closespider_itemcount`) that only partially
            # crawled the site, even though both report status "completed".
            "close_reason": status_data.get("close_reason"),
            # Structured failure token (issue #44): a stable discriminator
            # (unreachable / ssrf_blocked / crawl_error / spider_init_error /
            # seeding_incomplete) the corpus maps onto its own failure_class, instead of
            # scraping the humanized `error` prose. None unless the crawl failed with a
            # classified cause.
            "failure_reason": status_data.get("failure_reason"),
            # Block/restriction observability (from the status file's `blocking` section):
            # waf_challenge_count (Cloudflare wall) vs origin_forbidden_count (member-
            # restricted 403s), plus a full HTTP status_counts histogram. Defaulted to a
            # zeroed shape so a consumer always sees the same structure -- mid-crawl, or on
            # an older status file written before this field existed.
            "blocking": status_data.get("blocking") or {
                "waf_challenge_count": 0,
                "origin_forbidden_count": 0,
                "status_counts": {},
            },
            # Platform fingerprints from the first HTML response (corpus #112). Empty dict
            # default, same reason as `blocking`/`restrictions`: one stable shape for the
            # consumer whether mid-crawl or against an older crawler.
            "platform_signals": status_data.get("platform_signals") or {},
            # Restriction observability (issue #74): the URL classes we deliberately did not
            # fetch, and any robots.txt Crawl-delay we paced at. Zeroed default for the same
            # reason as `blocking` -- a consumer always sees the same shape, mid-crawl or
            # against an older crawler whose status file predates the field.
            "restrictions": status_data.get("restrictions") or {
                "robots_root_disallowed": None,
                # Kept in lockstep with ProgressWriter._robots_readability's default (#97
                # review): this literal is the OTHER producer of the same contract, and the
                # comment above promises one stable shape. A test pins the two key sets
                # together so they can only drift as a pair.
                "robots_readability": {
                    "outcome": "unknown",
                    "final_status": None,
                    "cf_wall": False,
                    "rules_from_state": False,
                },
                # Operator knobs (#99), same lockstep rule as the block above -- a test pins
                # this literal's key set against the status file's so the two producers can
                # only drift as a pair. It caught this one.
                "knobs": {
                    "robots_fetch_budget": {
                        "effective": None, "requested": None, "disposition": "unknown"},
                    "max_crawl_delay": {
                        "effective": None, "requested": None, "disposition": "unknown"},
                },
                "skipped": {
                    "robots_disallowed": 0,
                    "robots_disallowed_assets": 0,
                    "login_gated": 0,
                    "infra": 0,
                    "facet_capped": 0,
                    "nofollow_links": 0,
                    "meta_nofollow_pages": 0,
                },
                "crawl_delay": {
                    "applied": 0,
                    "honored_seconds": 0,
                    "requested_seconds": 0,
                },
            },
            "started_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.started_at))
            ),
            "elapsed_seconds": job.elapsed_seconds(),
        }

        # If status file shows running but monitor says completed/failed,
        # use the monitor's verdict (it read the status file after exit).
        if job.status == "completed":
            response["completed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(job.completed_at or time.time()),
            )
            response["result_url"] = f"/crawl/{job.job_id}/results"
        elif job.status == "failed":
            response["failed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(job.failed_at or time.time()),
            )
            response["error"] = _humanize_error(job.error)
        return response


# Map Scrapy close reasons to user-friendly messages.
_ERROR_MESSAGES = {
    "memusage_exceeded": "Crawl stopped: memory limit exceeded. Try a smaller site.",
    "cancel": "Crawl was cancelled.",
    "shutdown": "Service is restarting. Please retry.",
    "signal": "Crawl was interrupted.",
}


def _humanize_error(error: str | None) -> str:
    if not error:
        return "Crawl failed"
    return _ERROR_MESSAGES.get(error, error)
