"""Tests for run_spider.build_settings (settings wiring) and the CLI surface."""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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
        jobdir=None,
        proxy=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_proxy_registers_middleware_and_setting(monkeypatch):
    monkeypatch.delenv("YOKO_CRAWL_PROXY", raising=False)
    s = build_settings(make_args(proxy="http://user:pass@box:8080"))
    assert s["YOKO_CRAWL_PROXY"] == "http://user:pass@box:8080"
    mw = s["DOWNLOADER_MIDDLEWARES"]
    assert mw["proxy_middleware.ProxyMiddleware"] == 100
    # After the SSRF guard, so the target host is still vetted before the proxy hop.
    assert mw["ssrf_guard.SsrfGuardMiddleware"] < mw["proxy_middleware.ProxyMiddleware"]


def test_no_proxy_leaves_settings_clean(monkeypatch):
    monkeypatch.delenv("YOKO_CRAWL_PROXY", raising=False)
    s = build_settings(make_args())
    assert "YOKO_CRAWL_PROXY" not in s
    assert "proxy_middleware.ProxyMiddleware" not in s["DOWNLOADER_MIDDLEWARES"]


def test_proxy_read_from_env_when_no_flag(monkeypatch):
    # The job manager hands the proxy to the subprocess in YOKO_CRAWL_PROXY (off argv, so
    # embedded creds don't leak via `ps`); build_settings must honor it with no --proxy flag.
    monkeypatch.setenv("YOKO_CRAWL_PROXY", "http://user:pass@box:8080")
    s = build_settings(make_args(proxy=None))
    assert s["YOKO_CRAWL_PROXY"] == "http://user:pass@box:8080"
    assert s["DOWNLOADER_MIDDLEWARES"]["proxy_middleware.ProxyMiddleware"] == 100


def test_proxy_host_resolving_to_private_address_is_rejected(monkeypatch):
    # Defense-in-depth for non-API callers: a proxy pointing at an internal host must abort the
    # crawl here, not route egress through the private network. Fail closed (raise), never fall
    # back to a direct fetch.
    monkeypatch.delenv("YOKO_CRAWL_PROXY", raising=False)
    with patch("run_spider.host_resolves_to_blocked", return_value=True):
        with pytest.raises(ValueError, match="private/reserved"):
            build_settings(make_args(proxy="http://169.254.169.254:8080"))


def test_proxy_bad_scheme_rejected_at_subprocess_boundary(monkeypatch):
    # The scheme allowlist is re-applied here, not only in the API model, so a scripted/direct
    # invocation can't smuggle a non-proxy URL (file://, gopher://) into curl's proxy option.
    monkeypatch.delenv("YOKO_CRAWL_PROXY", raising=False)
    with pytest.raises(ValueError, match="scheme"):
        build_settings(make_args(proxy="file:///etc/passwd"))


def test_no_jobdir_setting_by_default():
    s = build_settings(make_args())
    assert "JOBDIR" not in s


def test_jobdir_setting_enables_scrapy_resume():
    s = build_settings(make_args(jobdir="/var/yoko-crawl/jobdirs/example.com"))
    assert s["JOBDIR"] == "/var/yoko-crawl/jobdirs/example.com"


def test_cookie_jar_enabled():
    # The jar lets the SITE's own session/affinity cookies persist across the crawl.
    s = build_settings(make_args())
    assert s["COOKIES_ENABLED"] is True


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
    # 403 is deliberately NOT retried: a Cloudflare 403 is a challenge/block that punching
    # through never actually cleared (IP-reputation driven), so retrying only re-hit the WAF.
    # Recording it once feeds the block-legibility counts instead. Transient codes still retry.
    assert 403 not in s["RETRY_HTTP_CODES"]
    assert 429 in s["RETRY_HTTP_CODES"]
    assert 503 in s["RETRY_HTTP_CODES"]
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


def test_impersonated_crawls_declare_their_own_download_timeout():
    """Issue #88: this path used to run on curl_cffi's undeclared 30s session default -- a
    bound nobody chose and one that moves when the dependency changes. The number has to be
    in this file."""
    s = build_settings(make_args(impersonate="chrome"))
    assert s["DOWNLOAD_TIMEOUT"] == 60


def test_the_impersonate_timeout_is_sized_for_serial_crawls():
    """Not Scrapy's 180s: `--impersonate` goes with WAF-fronted sites, crawled via
    `--profile presale` (delay >= 3 -> CONCURRENT_REQUESTS 1), where ONE hung request stalls
    the whole crawl for the full timeout."""
    s = build_settings(make_args(impersonate="chrome", profile="presale"))
    assert s["CONCURRENT_REQUESTS"] == 1
    assert s["DOWNLOAD_TIMEOUT"] == 60


def test_standard_crawls_are_untouched():
    """The default path already honoured DOWNLOAD_TIMEOUT; #88 must not change it.

    Asserts ABSENCE specifically. The first version allowed `or == 180`, whose second arm was
    dead -- `make_args()` defaults to `impersonate='off'`, and `build_settings` returns before
    the setting is ever added, so the key is simply not there."""
    s = build_settings(make_args())
    assert "DOWNLOAD_TIMEOUT" not in s
    assert "tls_impersonate.ImpersonateMiddleware" not in s["DOWNLOADER_MIDDLEWARES"]


def test_impersonate_without_presale_still_gets_the_declared_bound():
    """`--impersonate` and `--profile presale` are INDEPENDENT flags -- the API can send
    `{"impersonate": "chrome"}` alone -- so the 16-wide pairing must be covered too. The
    bound's justification leans on the serial case, but its APPLICABILITY must not."""
    s = build_settings(make_args(impersonate="chrome", profile="standard"))
    assert s["CONCURRENT_REQUESTS"] == 16
    assert s["DOWNLOAD_TIMEOUT"] == 60


class TestSpiderStartupFailure:
    """Issue #98. Scrapy routes a spider `__init__`/`from_crawler` exception into the Deferred
    that `CrawlerProcess.crawl()` returns. With no errback attached that Deferred goes
    UNHANDLED: Twisted logs the traceback at garbage-collection time (after anything main()
    prints, so not even reliably ordered), `process.start()` returns normally, and the script
    exits 0.

    Verified end to end on this tree by injecting a raise into `WebsiteSpider.__init__` and
    running the real `run_spider.py`: before the fix, exit 0 and no status file written; after,
    exit 1 and a `failed` status carrying `failure_reason: spider_init_error`."""

    def _run_main(self, tmp_path, spider_exc):
        """Drive the real `main()` with a CrawlerProcess whose spider blows up on construct."""
        import runpy
        status_file = tmp_path / "status.json"
        argv = ["run_spider.py", "--domain", "example.com",
                "--status-file", str(status_file), "--output", str(tmp_path / "o.jsonl")]

        real_process = None

        class _Process:
            def __init__(self, settings=None):
                nonlocal real_process
                real_process = self
                self._deferred = None

            def crawl(self, *a, **kw):
                from twisted.internet.defer import Deferred, fail
                from twisted.python.failure import Failure
                return fail(Failure(spider_exc)) if spider_exc else Deferred()

            def start(self):
                pass

        import run_spider
        with patch.object(run_spider, "CrawlerProcess", _Process), \
                patch.object(run_spider, "check_resolution_sync", lambda d: None), \
                patch.object(sys, "argv", argv):
            try:
                run_spider.main()
            except SystemExit as exc:
                return exc.code, status_file
        return 0, status_file

    def test_a_spider_that_cannot_construct_exits_nonzero(self, tmp_path):
        code, status_file = self._run_main(tmp_path, ValueError("construction exploded"))
        assert code == 1, (
            "exiting 0 here is what made job_manager report the crawl COMPLETED with zero "
            "pages -- indistinguishable from a site that genuinely had nothing"
        )

    def test_it_writes_a_classified_failed_status(self, tmp_path):
        import json
        code, status_file = self._run_main(tmp_path, ValueError("construction exploded"))
        data = json.loads(status_file.read_text())
        assert data["status"] == "failed"
        assert data["failure_reason"] == "spider_init_error", (
            "the corpus keys on failure_reason to classify; an unclassified failure reads "
            "as 'unclassified', not as 'the spider never started'"
        )
        assert "construction exploded" in data["error"], \
            "the operator needs the actual cause, not just that something failed"

    def test_a_healthy_start_neither_exits_nor_writes_a_failure(self, tmp_path):
        """The guard must not fire on the ordinary path."""
        code, status_file = self._run_main(tmp_path, None)
        assert code == 0
        assert not status_file.exists(), "no failure status should be written on a clean run"
