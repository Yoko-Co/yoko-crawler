"""Unit tests for check_urls.py (standalone bulk URL status checker).

Importing check_urls at collection time also guards the coupling to
tls_impersonate: a rename of CURRENT_TARGETS / user_agent_for breaks these
tests loudly instead of only failing at runtime.
"""

import csv

import pytest

import check_urls
from check_urls import _csv_safe, _retry_after, check, main, read_urls


class FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeSession:
    """Serves a scripted list of FakeResp objects / Exceptions per request()."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep retry tests instant."""
    monkeypatch.setattr(check_urls.time, "sleep", lambda _s: None)


def _check(session, method="GET", timeout=5, retries=1):
    return check(session, "https://example.com", "chrome131", "UA", method, timeout, retries)


# --- read_urls -------------------------------------------------------------

def test_read_urls_skips_blanks_and_comments_and_adds_scheme(tmp_path):
    f = tmp_path / "urls.txt"
    f.write_text("\n# a comment\nexample.com\nhttps://already.test/x\n   \n")
    assert list(read_urls(str(f))) == [
        "https://example.com",
        "https://already.test/x",
    ]


# --- check() retry state machine ------------------------------------------

def test_check_returns_status_on_success():
    s = FakeSession([FakeResp(200)])
    assert _check(s) == (200, "", "")
    assert s.calls == 1


def test_check_retries_past_block_then_passes():
    s = FakeSession([FakeResp(403), FakeResp(200)])
    assert _check(s, retries=1) == (200, "", "")
    assert s.calls == 2  # the blocked attempt warmed the cookie, retry passed


def test_check_exhausted_block_returns_code_with_neutral_note():
    s = FakeSession([FakeResp(503)])
    assert _check(s, retries=0) == (503, "", "blocked?")
    assert s.calls == 1


def test_check_exception_on_every_attempt_reports_last_exception():
    s = FakeSession([ValueError("boom-1"), ValueError("boom-2")])
    status, redirect, note = _check(s, retries=1)
    assert status == "ERROR"
    assert redirect == ""
    assert note == "ValueError: boom-2"  # the LAST exception, not a stale one
    assert s.calls == 2


def test_check_reports_redirect_location_without_following():
    s = FakeSession([FakeResp(301, {"Location": "https://example.com/new"})])
    assert _check(s) == (301, "https://example.com/new", "")


def test_check_3xx_missing_location_degrades_to_empty():
    s = FakeSession([FakeResp(302, {})])
    assert _check(s) == (302, "", "")


# --- _retry_after ----------------------------------------------------------

def test_retry_after_honors_integer_seconds():
    assert _retry_after(FakeResp(503, {"Retry-After": "5"})) == 5


def test_retry_after_caps_hostile_value():
    assert _retry_after(FakeResp(503, {"Retry-After": "99999"})) == check_urls._MAX_RETRY_AFTER_SECONDS


def test_retry_after_falls_back_on_missing_or_http_date():
    default = check_urls._RETRY_BACKOFF_SECONDS
    assert _retry_after(FakeResp(503, {})) == default
    assert _retry_after(FakeResp(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) == default


# --- _csv_safe -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["=cmd|'/c calc'!A1", "+1", "-1", "@SUM(A1)", "\ttab", "\rcr"])
def test_csv_safe_neutralizes_formula_triggers(bad):
    assert _csv_safe(bad) == "'" + bad


def test_csv_safe_leaves_normal_urls_untouched():
    assert _csv_safe("https://example.com/x") == "https://example.com/x"
    assert _csv_safe("") == ""


def test_output_csv_neutralizes_malicious_location(tmp_path, monkeypatch):
    """End-to-end: a probed host returning a formula Location lands neutralized."""
    monkeypatch.setattr(
        check_urls.requests, "Session",
        lambda: FakeSession([FakeResp(302, {"Location": "=HYPERLINK(0)"})]),
    )
    urls = tmp_path / "urls.txt"
    urls.write_text("example.com\n")
    out = tmp_path / "out.csv"
    assert main([str(urls), "-o", str(out)]) == 0
    rows = list(csv.reader(out.open()))
    assert rows[0] == ["url", "status", "redirect_to", "note"]
    assert rows[1][2] == "'=HYPERLINK(0)"  # apostrophe-prefixed


# --- argument validation ---------------------------------------------------

def test_negative_retries_is_rejected(tmp_path):
    f = tmp_path / "urls.txt"
    f.write_text("example.com\n")
    with pytest.raises(SystemExit):
        main([str(f), "--retries", "-1"])
