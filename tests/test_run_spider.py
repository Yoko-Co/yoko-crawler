"""Tests for run_spider.build_settings (settings wiring)."""

from types import SimpleNamespace

from run_spider import build_settings


def make_args(**overrides):
    base = dict(
        output="out.jsonl",
        format="jsonlines",
        delay=1.0,
        user_agent=None,
        impersonate="off",
        status_file="status.json",
        emit_content=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_ssrf_guard_registered_without_impersonation():
    s = build_settings(make_args(impersonate="off"))
    assert "ssrf_guard.SsrfGuardMiddleware" in s["DOWNLOADER_MIDDLEWARES"]
    assert "DOWNLOAD_HANDLERS" not in s  # standard Scrapy TLS


def test_ssrf_guard_survives_impersonation():
    # Regression: the impersonate branch must ADD its middleware, not replace the
    # dict and silently drop the SSRF guard.
    s = build_settings(make_args(impersonate="chrome"))
    mw = s["DOWNLOADER_MIDDLEWARES"]
    assert "ssrf_guard.SsrfGuardMiddleware" in mw
    assert "tls_impersonate.ImpersonateMiddleware" in mw
    assert s["RETRY_TIMES"] == 1
    assert 403 in s["RETRY_HTTP_CODES"]
    assert s["IMPERSONATE_TARGET"] == "chrome"
    # Middleware supplies a per-request UA matching the fingerprint.
    assert s["USER_AGENT"] is None


def test_explicit_user_agent_preserved_when_impersonating():
    s = build_settings(make_args(impersonate="chrome", user_agent="custom-agent"))
    assert s["USER_AGENT"] == "custom-agent"


def test_non_impersonate_uses_default_chrome_ua():
    s = build_settings(make_args(impersonate="off"))
    assert "Chrome" in s["USER_AGENT"]


def test_feed_fields_include_enrichment_but_not_content_by_default():
    s = build_settings(make_args(emit_content=False))
    fields = s["FEED_EXPORT_FIELDS"]
    # Original five preserved at the front, in order.
    assert fields[:5] == ["url", "status", "last_modified", "redirected_to", "referrer"]
    # Additive enrichment columns present.
    for f in ("content_hash", "word_count", "iframe_hosts", "embed_count_nonbenign"):
        assert f in fields
    # content_text is opt-in.
    assert "content_text" not in fields


def test_emit_content_appends_content_text_column():
    s = build_settings(make_args(emit_content=True))
    assert "content_text" in s["FEED_EXPORT_FIELDS"]
