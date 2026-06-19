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
    monkeypatch.setattr(
        ssrf_guard,
        "host_resolves_to_blocked",
        lambda host: pytest.fail("should not resolve a hostless URL"),
    )
    mw = SsrfGuardMiddleware()
    assert mw.process_request(make_request("data:text/plain,hi"), spider=None) is None
