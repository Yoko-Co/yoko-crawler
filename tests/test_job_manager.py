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


@pytest.fixture(autouse=True)
def use_tmp_results_dir(tmp_path, monkeypatch):
    """Use a temp directory for RESULTS_DIR in all job manager tests."""
    monkeypatch.setattr("job_manager.RESULTS_DIR", tmp_path)
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
            status="running",
            started_at=time.time() - 60,
        )
        jm._jobs["abc123def456789a"] = job

        response = await jm.get_status_response(job)
        assert response["job_id"] == "abc123def456789a"
        assert response["domain"] == "example.com"
        assert response["status"] == "running"
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
