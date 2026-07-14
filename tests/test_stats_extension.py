"""Tests for stats_extension.ProgressWriter (status file + SSRF-empty guard).

An all-403/blocked crawl is NOT failed here: the crawl completes and emits its 403
rows, and the consumer (yoko-corpus) owns the blocked-crawl policy (retry with browser
impersonation, then an honest "we couldn't read this" report). Only a crawl that fetched
NOTHING (SSRF guard dropped every host) is failed as a genuine empty result.
"""

import json

from stats_extension import ProgressWriter


class FakeStats:
    def __init__(self, values):
        self._values = values

    def get_value(self, key, default=0):
        return self._values.get(key, default)


def _write_and_read(tmp_path, stats_values, reason):
    status_file = str(tmp_path / "status.json")
    writer = ProgressWriter(FakeStats(stats_values), status_file)
    writer.spider_closed(spider=None, reason=reason)
    with open(status_file) as f:
        return json.load(f)


def test_all_403_completes_consumer_owns_policy(tmp_path):
    # A wholesale bot-block (every response 403) COMPLETES, emitting its 403 rows -- the
    # corpus reads the forbidden ratio to retry with impersonation / report honestly.
    # Failing here would deny it both. (Was: impersonated all-403 -> failed.)
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 50},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["error"] is None


def test_partial_403_completes(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 10},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["error"] is None


def test_all_redirects_completes(tmp_path):
    # A legit redirect-only (or 404-only) crawl has zero 200s -- it completes.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 0},
        reason="finished",
    )
    assert data["status"] == "completed"


def test_ssrf_blocked_into_emptiness_marked_failed(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"ssrf_guard/blocked": 3, "response_received_count": 0},
        reason="finished",
    )
    assert data["status"] == "failed"
    assert "SSRF guard" in data["error"]


def test_ssrf_block_with_fetched_pages_completes(tmp_path):
    # Dropped a stray internal link but fetched real pages -> the crawl is fine.
    data = _write_and_read(
        tmp_path,
        {"ssrf_guard/blocked": 1, "response_received_count": 20},
        reason="finished",
    )
    assert data["status"] == "completed"


def test_failure_reason_preserved(tmp_path):
    # A real non-completed close reason (e.g. OOM) still fails and is surfaced verbatim.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 10, "downloader/response_status_count/403": 2},
        reason="memusage_exceeded",
    )
    assert data["status"] == "failed"
    assert data["error"] == "memusage_exceeded"


def test_close_reason_surfaced_on_natural_finish(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 20, "scheduler/enqueued": 20},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["close_reason"] == "finished"


def test_close_reason_surfaced_on_safety_valve_stop(tmp_path):
    # A capped crawl reports "completed" but the close_reason marks it partial, and
    # discovered > crawled shows how much was left unfetched.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 1200, "scheduler/enqueued": 5000},
        reason="closespider_timeout",
    )
    assert data["status"] == "completed"
    assert data["close_reason"] == "closespider_timeout"
    assert data["urls_crawled"] == 1200
    assert data["urls_discovered"] == 5000
