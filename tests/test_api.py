"""Integration tests for the FastAPI endpoints."""

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# Set required env var before importing app.
os.environ["YOKO_CRAWL_API_KEY"] = "a" * 48

from job_manager import RESULTS_DIR, Job, JobManager
from main import app


@pytest.fixture
def api_key():
    return "a" * 48


@pytest.fixture
def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # Use temp directory for results so tests work outside Docker.
    monkeypatch.setattr("job_manager.RESULTS_DIR", tmp_path)

    # Manually set up app state since ASGITransport doesn't run lifespan.
    app.state.job_manager = JobManager(max_concurrent=3)
    app.state.start_time = time.time()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestHealthEndpoint:
    async def test_health_no_auth(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "active_jobs" in data
        assert "uptime_seconds" in data


class TestStartCrawl:
    async def test_missing_auth(self, client):
        response = await client.post("/crawl", json={"domain": "example.com"})
        # HTTPBearer returns 403 when header is missing entirely.
        assert response.status_code in (401, 403)

    async def test_invalid_auth(self, client):
        response = await client.post(
            "/crawl",
            json={"domain": "example.com"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    async def test_invalid_domain(self, client, auth_headers):
        response = await client.post(
            "/crawl",
            json={"domain": "not a domain!"},
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_start_crawl_success(self, client, auth_headers):
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345

        async def mock_wait():
            mock_process.returncode = 0
            return 0

        mock_process.wait = mock_wait
        mock_process.terminate = MagicMock()

        mock_results = [(2, 1, 6, "", ("93.184.216.34", 443))]

        with patch("domain_validator.asyncio.get_running_loop") as mock_loop, \
             patch("job_manager.asyncio.create_subprocess_exec", return_value=mock_process):
            mock_loop.return_value.getaddrinfo = AsyncMock(return_value=mock_results)
            response = await client.post(
                "/crawl",
                json={"domain": "example.com"},
                headers=auth_headers,
            )

        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["status"] in ("running", "queued")


class TestGetStatus:
    async def test_invalid_job_id_format(self, client, auth_headers):
        response = await client.get(
            "/crawl/not-a-valid-id",
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_job_not_found(self, client, auth_headers):
        response = await client.get(
            "/crawl/abcdef0123456789",
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestDeleteJob:
    async def test_delete_nonexistent_is_ok(self, client, auth_headers):
        """DELETE is idempotent — deleting a non-existent job returns 200."""
        response = await client.delete(
            "/crawl/abcdef0123456789",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json() == {"deleted": True}


class TestGetResults:
    async def test_results_not_found(self, client, auth_headers):
        response = await client.get(
            "/crawl/abcdef0123456789/results",
            headers=auth_headers,
        )
        assert response.status_code == 404
