"""Tests for stats_extension.ProgressWriter (status file + stale-fingerprint guard)."""

import json

from stats_extension import ProgressWriter


class FakeStats:
    def __init__(self, values):
        self._values = values

    def get_value(self, key, default=0):
        return self._values.get(key, default)


def _write_and_read(tmp_path, stats_values, impersonate, reason):
    status_file = str(tmp_path / "status.json")
    writer = ProgressWriter(FakeStats(stats_values), status_file, impersonate=impersonate)
    writer.spider_closed(spider=None, reason=reason)
    with open(status_file) as f:
        return json.load(f)


def test_impersonated_all_403_marked_failed(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 50},
        impersonate="chrome",
        reason="finished",
    )
    assert data["status"] == "failed"
    assert "all 403" in data["error"]


def test_impersonated_partial_403_completes(tmp_path):
    # Some 403s but not all -> the crawl got usable results; not a blocked crawl.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 10},
        impersonate="chrome",
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["error"] is None


def test_impersonated_all_redirects_completes(tmp_path):
    # Regression: a legit redirect-only (or 404-only) crawl has zero 200s but
    # zero 403s -- it must NOT be mistaken for a blocked crawl.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 0},
        impersonate="chrome",
        reason="finished",
    )
    assert data["status"] == "completed"


def test_non_impersonated_all_403_still_completes(tmp_path):
    # The guard only applies to impersonated crawls; a normal crawl that the site
    # blocks is the caller's signal to read the 403 rows, not a tooling failure.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 50},
        impersonate=None,
        reason="finished",
    )
    assert data["status"] == "completed"


def test_guard_does_not_fire_when_no_requests_made(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 0, "downloader/response_status_count/403": 0},
        impersonate="chrome",
        reason="finished",
    )
    assert data["status"] == "completed"


def test_failure_reason_preserved(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 10, "downloader/response_status_count/403": 2},
        impersonate="chrome",
        reason="memusage_exceeded",
    )
    assert data["status"] == "failed"
    assert data["error"] == "memusage_exceeded"
