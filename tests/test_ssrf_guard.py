"""Tests for ssrf_guard.SsrfGuardMiddleware."""

from types import SimpleNamespace

import pytest
from scrapy.exceptions import IgnoreRequest

import ssrf_guard
from ssrf_guard import SsrfGuardMiddleware


def make_request(url):
    return SimpleNamespace(url=url)


class FakeStats:
    def __init__(self):
        self.values = {}

    def inc_value(self, key, count=1, start=0):
        self.values[key] = self.values.get(key, start) + count


def test_drops_request_to_blocked_host(monkeypatch):
    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", lambda host: True)
    mw = SsrfGuardMiddleware()
    with pytest.raises(IgnoreRequest):
        mw.process_request(make_request("https://internal.test/page"), spider=None)


def test_allows_request_to_public_host(monkeypatch):
    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", lambda host: False)
    mw = SsrfGuardMiddleware()
    assert mw.process_request(make_request("https://example.com/"), spider=None) is None


def test_blocked_request_increments_stat(monkeypatch):
    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", lambda host: True)
    stats = FakeStats()
    mw = SsrfGuardMiddleware(stats=stats)
    with pytest.raises(IgnoreRequest):
        mw.process_request(make_request("https://internal.test/"), spider=None)
    assert stats.values.get("ssrf_guard/blocked") == 1


def test_caches_resolution_per_host(monkeypatch):
    calls = []

    def fake(host):
        calls.append(host)
        return False

    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", fake)
    mw = SsrfGuardMiddleware()
    mw.process_request(make_request("https://example.com/a"), spider=None)
    mw.process_request(make_request("https://example.com/b"), spider=None)
    assert calls == ["example.com"]  # resolved once, then cached


def test_url_without_host_passes(monkeypatch):
    """A hostless URL must not reach DNS resolution.

    This used to use `data:text/plain,hi`, which issue #89 now refuses outright at the
    scheme check -- a deliberate contract change, not a regression, so the example moved to
    a hostless *http* URL. The property under test is unchanged: no resolution attempt.
    Refusal of `data:` itself is asserted in test_refuses_non_http_scheme_even_on_a_public_host.
    """
    monkeypatch.setattr(
        ssrf_guard,
        "host_resolves_to_blocked",
        lambda host: pytest.fail("should not resolve a hostless URL"),
    )
    mw = SsrfGuardMiddleware()
    assert mw.process_request(make_request("http:///just-a-path"), spider=None) is None


def test_refuses_non_http_scheme_even_on_a_public_host(monkeypatch):
    """Issue #89. The host check cannot do this job: `file://<public-domain>/etc/passwd`
    has a hostname that resolves perfectly well, and Scrapy's FileDownloadHandler ignores
    that host and reads the local path. "The host is fine" and "safe to hand to a download
    handler" are different questions; only the first was being asked."""
    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", lambda host: False)
    mw = SsrfGuardMiddleware()
    for url in (
        "file://example.com/etc/passwd",
        "ftp://example.com/x",
        "s3://example.com/x",
        "data:text/plain,hi",
    ):
        with pytest.raises(IgnoreRequest):
            mw.process_request(make_request(url), spider=None)


def test_scheme_refusal_is_counted_separately_from_a_blocked_address(monkeypatch):
    """Distinct stat: "we refused a scheme" and "this host resolves somewhere reserved" are
    different operator stories, and collapsing them would hide a site probing for one."""
    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", lambda host: False)
    stats = FakeStats()
    mw = SsrfGuardMiddleware(stats=stats)
    with pytest.raises(IgnoreRequest):
        mw.process_request(make_request("file://example.com/etc/passwd"), spider=None)
    assert stats.values.get("ssrf_guard/blocked_scheme") == 1
    assert "ssrf_guard/blocked" not in stats.values


def test_scheme_check_runs_before_dns_resolution(monkeypatch):
    """A refused scheme must not cost a DNS lookup, and must not depend on one succeeding."""
    calls = []

    def _boom(host):
        calls.append(host)
        raise AssertionError("resolution must not be reached for a refused scheme")

    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", _boom)
    mw = SsrfGuardMiddleware()
    with pytest.raises(IgnoreRequest):
        mw.process_request(make_request("file://example.com/etc/passwd"), spider=None)
    assert calls == []


def test_http_and_https_still_pass(monkeypatch):
    monkeypatch.setattr(ssrf_guard, "host_resolves_to_blocked", lambda host: False)
    mw = SsrfGuardMiddleware()
    for url in ("http://example.com/", "https://example.com/", "HTTPS://example.com/"):
        assert mw.process_request(make_request(url), spider=None) is None
