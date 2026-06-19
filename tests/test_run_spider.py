"""Tests for run_spider.build_settings (settings wiring) and the CLI surface."""

import os
import subprocess
import sys
from types import SimpleNamespace

from run_spider import build_settings

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_args(**overrides):
    base = dict(
        output="out.jsonl",
        format="jsonlines",
        delay=1.0,
        user_agent=None,
        impersonate="off",
        status_file="status.json",
        emit_content=False,
        profile="standard",
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


def test_standard_profile_uses_configured_delay_and_concurrency():
    s = build_settings(make_args(profile="standard", delay=1.0))
    assert s["DOWNLOAD_DELAY"] == 1.0
    assert s["CONCURRENT_REQUESTS"] == 16
    assert s["AUTOTHROTTLE_TARGET_CONCURRENCY"] == 2.0


def test_presale_profile_forces_serial_polite_mode():
    # presale forces a >=3s delay -> serial mode, regardless of the passed delay.
    s = build_settings(make_args(profile="presale", delay=1.0))
    assert s["DOWNLOAD_DELAY"] == 3.0
    assert s["CONCURRENT_REQUESTS"] == 1
    assert s["AUTOTHROTTLE_TARGET_CONCURRENCY"] == 1.0
    # Max delay derives from the floored delay (max(30, 3*10)).
    assert s["AUTOTHROTTLE_MAX_DELAY"] == 30


def test_presale_does_not_lower_a_higher_delay():
    s = build_settings(make_args(profile="presale", delay=5.0))
    assert s["DOWNLOAD_DELAY"] == 5.0
    assert s["CONCURRENT_REQUESTS"] == 1
    # max(30, 5*10) == 50.
    assert s["AUTOTHROTTLE_MAX_DELAY"] == 50


def test_download_maxsize_bounds_hostile_responses():
    s = build_settings(make_args())
    assert s["DOWNLOAD_MAXSIZE"] == 64 * 1024 * 1024


def test_presale_keeps_ssrf_guard():
    # Politeness must never relax the SSRF guard.
    s = build_settings(make_args(profile="presale"))
    assert "ssrf_guard.SsrfGuardMiddleware" in s["DOWNLOADER_MIDDLEWARES"]


def test_cli_help_lists_new_flags():
    result = subprocess.run(
        [sys.executable, "run_spider.py", "--help"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0
    assert "--profile" in result.stdout
    assert "--emit-content" in result.stdout


def test_cli_rejects_invalid_profile():
    result = subprocess.run(
        [
            sys.executable,
            "run_spider.py",
            "--domain",
            "example.com",
            "--output",
            "/tmp/out.jsonl",
            "--status-file",
            "/tmp/status.json",
            "--profile",
            "aggressive",
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    # argparse rejects the bad choice during parsing, before any crawl starts.
    assert result.returncode != 0
    assert "profile" in result.stderr.lower()
