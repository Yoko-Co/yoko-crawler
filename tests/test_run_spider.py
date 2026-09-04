"""Tests for run_spider.build_settings (settings wiring) and the CLI surface."""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run_spider
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

    def _run_main(self, tmp_path, spider_exc, status_file=None, jobdir=None):
        """Drive the real `main()` with a CrawlerProcess whose spider blows up on construct."""
        status_file = status_file or (tmp_path / "status.json")
        argv = ["run_spider.py", "--domain", "example.com",
                "--status-file", str(status_file), "--output", str(tmp_path / "o.jsonl")]
        if jobdir is not None:
            argv += ["--jobdir", str(jobdir)]

        real_process = None

        class _Crawler:
            """Carries a REAL SignalManager so the spider_opened connect main() performs is
            the one under test -- including whether its receiver survives garbage collection."""

            def __init__(self):
                from scrapy.signalmanager import SignalManager
                # `SignalManager(self)` mirrors the real Crawler (scrapy/crawler.py). The bare
                # `SignalManager()` defaults to `dispatcher.Anonymous`, a PROCESS-GLOBAL
                # bucket: `getReceivers` then returns receivers connected by unrelated tests,
                # and the weak=False assertion below passes on someone else's registration
                # depending on collection order.
                self.signals = SignalManager(self)

        class _Process:
            def __init__(self, settings=None):
                nonlocal real_process
                real_process = self
                self._deferred = None
                # Real CrawlerProcess exposes the crawlers it is running; run_spider connects
                # spider_opened on each to clear the JOBDIR stall marker.
                # `test_crawler_process_really_exposes_crawlers` pins the real attribute.
                self.crawlers = []

            def crawl(self, *a, **kw):
                from twisted.internet.defer import Deferred, fail
                from twisted.python.failure import Failure
                # Populated HERE, not in __init__, because CrawlerRunner._crawl adds the
                # crawler when crawl() is called. main() must connect spider_opened after
                # process.crawl(); a fake that is pre-populated would hide that ordering.
                self.crawlers.append(_Crawler())
                return fail(Failure(spider_exc)) if spider_exc else Deferred()

            def start(self):
                pass

        with patch.object(run_spider, "CrawlerProcess", _Process), \
                patch.object(run_spider, "check_resolution_sync", lambda d: None), \
                patch.object(sys, "argv", argv):
            try:
                run_spider.main()
            except SystemExit as exc:
                self._last_process = real_process
                return exc.code, status_file
        self._last_process = real_process
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

    def test_it_refuses_to_clobber_a_status_the_spider_already_wrote(self, tmp_path):
        """Defence in depth, and deliberately unreachable today: measured on Scrapy 2.18 this
        Deferred errbacks only at or before `spider_opened`, so a post-open failure never gets
        here (a `start()` raise and a callback raise were both confirmed NOT to fire it). But
        that is a property of Scrapy internals, not a contract -- two reviewers independently
        worried about the coupling. If a future version routed a post-open failure here, the
        unguarded code would overwrite a real `completed` status with `urls_crawled: 0` and
        mislabel it `spider_init_error`. One stat removes the whole class."""
        import json
        status_file = tmp_path / "status.json"
        status_file.write_text(json.dumps({"status": "completed", "urls_crawled": 6}))
        code, _ = self._run_main(
            tmp_path, ValueError("late failure"), status_file=status_file)
        assert code == 1, "it must still fail loudly"
        data = json.loads(status_file.read_text())
        assert data["status"] == "completed" and data["urls_crawled"] == 6, (
            "the spider's own report of what it actually did outranks a late handler's guess"
        )
        assert "failure_reason" not in data

    def test_mains_spider_opened_receiver_is_registered_STRONGLY(self, tmp_path):
        """pydispatch stores receivers WEAKLY by default, so `signals.connect(fn)` on a
        function with no other reference is collected before `spider_opened` ever fires: the
        stall marker is silently never cleared, and the next run discards an innocent frontier.
        A first draft of this change had exactly that bug.

        Asserting on the REGISTRY rather than on garbage collection is deliberate. The obvious
        test -- call main(), gc.collect(), fire the signal -- passes either way, because pytest
        keeps main()'s frame alive and with it the closure. It looked like a real test and
        proved nothing; this one fails the moment `weak=False` is dropped."""
        import weakref
        from pydispatch import dispatcher
        from scrapy import signals as scrapy_signals

        jobdir = tmp_path / "job"
        jobdir.mkdir()
        self._run_main(tmp_path, None, jobdir=jobdir)
        crawler = self._last_process.crawlers[0]

        receivers = list(dispatcher.getReceivers(
            crawler.signals.sender, scrapy_signals.spider_opened
        ))
        # Name the receiver rather than trusting the bucket to be ours: a shared sender would
        # otherwise let an unrelated test's strong registration satisfy this assertion.
        ours = [r for r in receivers
                if getattr(r, "__name__", None) == "_clear_marker_on_open"]
        assert ours, (
            "main() did not connect its own _clear_marker_on_open to spider_opened "
            f"(receivers seen: {receivers!r})"
        )
        assert not any(isinstance(r, weakref.ref) for r in ours), (
            "main() connected its spider_opened receiver WEAKLY. Nothing else holds that "
            "closure, so it dies before the signal fires and the JOBDIR stall marker is never "
            "cleared -- a spider that opened fine still costs its domain the frontier. "
            "Pass weak=False."
        )

    def test_failing_to_start_MARKS_the_jobdir_that_was_in_play(self, tmp_path):
        """The middle of the chain. `strike_jobdir` being correct proves nothing
        unless the failure path actually calls it -- without this, job_manager keeps the
        JOBDIR and nothing ever records that it was implicated."""
        jobdir = tmp_path / "job"
        jobdir.mkdir()
        (jobdir / "requests.seen").write_text("frontier")

        code, _ = self._run_main(tmp_path, ValueError("construction exploded"), jobdir=jobdir)

        assert code == 1
        assert (jobdir / "requests.seen").exists(), (
            "a FIRST failure to open discarded the frontier -- that is the multi-session crawl "
            "#103 exists to preserve"
        )
        assert run_spider.strike_jobdir(str(jobdir)) == "discarded", (
            "the run left no strike on the frontier it was holding, so the next attempt trusts "
            "it again -- and if that frontier is why the spider could not open, every attempt "
            "fails identically forever"
        )

    def test_a_weak_connect_really_does_drop_the_receiver(self):
        """Why `weak=False` above is load-bearing, pinned against pydispatch itself rather
        than asserted in a comment. If this ever fails, the default became safe and the
        registry assertion above can be relaxed."""
        import gc
        from scrapy import signals as scrapy_signals
        from scrapy.signalmanager import SignalManager

        def _build(weak):
            fired = []

            def _receiver(*_a, **_kw):
                fired.append(1)

            mgr = SignalManager()
            kwargs = {} if weak else {"weak": False}
            mgr.connect(_receiver, signal=scrapy_signals.spider_opened, **kwargs)
            return mgr, fired

        weak_mgr, weak_fired = _build(weak=True)
        strong_mgr, strong_fired = _build(weak=False)
        gc.collect()
        weak_mgr.send_catch_log(signal=scrapy_signals.spider_opened)
        strong_mgr.send_catch_log(signal=scrapy_signals.spider_opened)

        assert not weak_fired, "a weakly-connected local function unexpectedly survived"
        assert strong_fired, "weak=False failed to keep the receiver alive"

    def test_a_healthy_start_neither_exits_nor_writes_a_failure(self, tmp_path):
        """The guard must not fire on the ordinary path."""
        code, status_file = self._run_main(tmp_path, None)
        assert code == 0
        assert not status_file.exists(), "no failure status should be written on a clean run"


class TestStartupFailureIntegration:
    """The mocked tests above stub `CrawlerProcess`, so they pin our own handling but say
    nothing about WHERE Scrapy actually routes a construction failure -- the exact thing the
    fix depends on, and the exact thing a Scrapy upgrade could move (#98 review). These run
    the real `run_spider.py` in a subprocess against a real spider that raises."""

    def _run(self, tmp_path, *, in_init="", in_class=""):
        """Copy the tree, inject into WebsiteSpider, run the real run_spider.py."""
        import shutil
        tree = tmp_path / "tree"
        shutil.copytree(_REPO_ROOT, tree, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".venv", "*.pyc", "tests"))
        spider = tree / "website_spider.py"
        src = spider.read_text()
        if in_init:
            anchor = "        self.robots_download_timeout = self._resolve_robots_timeout()"
            assert src.count(anchor) == 1, "init anchor moved -- re-point this test"
            src = src.replace(anchor, anchor + "\n" + in_init, 1)
        if in_class:
            anchor = "class WebsiteSpider(scrapy.Spider):\n"
            assert src.count(anchor) == 1, "class anchor moved -- re-point this test"
            src = src.replace(anchor, anchor + in_class + "\n", 1)
        spider.write_text(src)

        status_file = tmp_path / "status.json"
        proc = subprocess.run(
            [sys.executable, "run_spider.py", "--domain", "example.com",
             "--status-file", str(status_file), "--output", str(tmp_path / "o.jsonl")],
            cwd=tree, capture_output=True, text=True, timeout=180,
        )
        return proc, status_file

    def test_a_real_construction_failure_exits_1_and_classifies(self, tmp_path):
        """Guard 1 against the REAL Scrapy call path, not a stubbed CrawlerProcess."""
        import json
        proc, status_file = self._run(
            tmp_path, in_init='        raise ValueError("injected construction failure")')
        assert proc.returncode == 1, (
            f"exit 0 is the #98 bug -- job_manager reads it as a completed crawl. "
            f"stderr: {proc.stderr[-600:]}"
        )
        assert status_file.exists(), "no status file means job_manager falls back to its stub"
        data = json.loads(status_file.read_text())
        assert data["status"] == "failed"
        assert data["failure_reason"] == "spider_init_error"
        assert "injected construction failure" in data["error"]

    def test_a_synchronous_raise_before_the_deferred_is_also_classified(self, tmp_path):
        """`update_settings` is called from `Crawler.__init__`, so it raises straight out of
        `process.crawl()` -- BEFORE any Deferred exists to attach an errback to. This exited 1
        with NO classified status, reproducing #98's shape through a different door (#98
        review). The whole point of the fix is that a failure to start cannot look like
        anything else, so a second door had to be closed too."""
        import json
        proc, status_file = self._run(tmp_path, in_class=(
            "    @classmethod\n"
            "    def update_settings(cls, settings):\n"
            '        raise RuntimeError("injected settings failure")\n'
        ))
        assert proc.returncode == 1, f"stderr: {proc.stderr[-600:]}"
        assert status_file.exists(), (
            "a synchronous raise wrote no status at all, so job_manager saw only its own "
            "queued stub -- exactly the state #98 is about"
        )
        data = json.loads(status_file.read_text())
        assert data["status"] == "failed"
        assert data["failure_reason"] == "spider_init_error"
        assert "injected settings failure" in data["error"]


class TestStatusAlreadyAdvanced:
    """Pins `_status_already_advanced`, the one-stat check that stops a late failure handler
    overwriting the spider's own account of what it did (#98 review)."""

    def _check(self, tmp_path, content):
        from run_spider import _status_already_advanced
        f = tmp_path / "s.json"
        if content is not None:
            f.write_text(content)
        return _status_already_advanced(f)

    def test_the_pre_spawn_stub_is_not_advanced(self, tmp_path):
        assert self._check(tmp_path, '{"status": "queued"}') is False

    def test_a_missing_or_corrupt_file_is_not_advanced(self, tmp_path):
        assert self._check(tmp_path, None) is False
        assert self._check(tmp_path, "not json{{") is False
        assert self._check(tmp_path, '["a", "list"]') is False

    def test_any_status_the_spider_wrote_counts_as_advanced(self, tmp_path):
        for status in ("running", "completed", "failed"):
            assert self._check(tmp_path, '{"status": "%s"}' % status) is True, status


_ENV_SKIP_DIRS = {"tests", ".venv", "__pycache__", ".git", "docs"}


def _env_vars_in_code(root):
    import re
    found = set()
    for path in root.rglob("*.py"):
        if _ENV_SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        found |= set(re.findall(r"YOKO_[A-Z_]+", path.read_text()))
    return found


def _env_vars_in_readme_table(readme_text):
    """Only the Configuration TABLE counts, not the whole file.

    Grepping the whole README made this a silent pass: a var mentioned anywhere in prose --
    a shell snippet, a deployment note -- satisfied it, so the actual table row could be
    deleted and the suite stayed green. Demonstrated in review (#99)."""
    import re
    rows = [ln for ln in readme_text.splitlines() if ln.lstrip().startswith("| `YOKO_")]
    return set(re.findall(r"YOKO_[A-Z_]+", "\n".join(rows)))


def test_every_env_knob_is_documented_in_the_readme():
    """The stale-table defect, made impossible rather than fixed again (#99).

    Five of nine `YOKO_*` variables were undocumented when #99 was filed, and three separate
    reviews in this series each found a stale list somewhere. A list nothing checks goes stale
    by default; this is the check.

    Deliberately one-directional: it fails on an env var the code reads and the README table
    does not list, not on a table row without a matching read. Documenting something the code
    stopped reading is a much smaller problem than the reverse, and a two-way check would
    fight legitimate prose."""
    from pathlib import Path
    root = Path(_REPO_ROOT)
    missing = sorted(_env_vars_in_code(root)
                     - _env_vars_in_readme_table((root / "README.md").read_text()))
    assert not missing, (
        f"env variables read by the code but absent from README's Configuration TABLE: "
        f"{missing}. An operator has no way to discover these; add a row rather than "
        f"deleting this assertion."
    )


def test_the_documentation_tripwire_actually_bites():
    """A tripwire with no self-test is a shape that looks like a check. Both of this one's
    original blind spots -- whole-file grep, and a root-only glob -- passed while the gap they
    exist to close was open, so the check itself needs checking (#99 review)."""
    from pathlib import Path
    root = Path(_REPO_ROOT)
    readme = (root / "README.md").read_text()

    # A var present in prose but NOT as a table row must not count as documented.
    prose_only = readme.replace("| `YOKO_CRAWL_API_KEY`", "| `YOKO_SOMETHING_ELSE`")
    assert "YOKO_CRAWL_API_KEY" in prose_only, "fixture assumption: it appears in prose too"
    assert "YOKO_CRAWL_API_KEY" not in _env_vars_in_readme_table(prose_only), (
        "prose mentions must not satisfy the table check -- that was the silent pass"
    )

    # And the code scan must reach beyond the top level.
    assert _env_vars_in_code(root), "the code scan found nothing at all"


class TestUnopenableJobdirSelfHeals:
    """#103 review. `job_manager` keeps the JOBDIR on `spider_init_error`. A frontier Scrapy
    cannot READ produces that same token -- `Scheduler.open()` runs inside `open_spider_async`,
    whose only `except` is `CloseSpider`, and `Crawler.crawl` re-raises -- so without a bound
    the keep would re-read the bad frontier every session forever."""

    def test_the_first_strike_LEAVES_the_frontier_intact(self, tmp_path):
        """The whole point of #103: a spider that never opened usually never touched the
        frontier, so one failure must not cost a multi-session crawl."""
        jobdir = tmp_path / "job"
        jobdir.mkdir()
        (jobdir / "requests.seen").write_text("frontier")

        assert run_spider.strike_jobdir(str(jobdir)) == "marked"
        assert (jobdir / "requests.seen").exists()

    def test_the_second_consecutive_strike_DISCARDS_it(self, tmp_path):
        """Two failures to open against the same frontier is the shape a bad frontier makes.
        Without this the keep re-reads it forever and the domain is bricked."""
        jobdir = tmp_path / "job"
        jobdir.mkdir()
        (jobdir / "requests.seen").write_text("frontier")

        run_spider.strike_jobdir(str(jobdir))
        assert run_spider.strike_jobdir(str(jobdir)) == "discarded"
        assert not jobdir.exists()

    def test_opening_the_spider_RESETS_the_count(self, tmp_path):
        """An init failure with an INNOCENT jobdir (bad proxy, rejected impersonation profile)
        must not put the domain one strike from losing its frontier forever."""
        jobdir = tmp_path / "job"
        jobdir.mkdir()
        (jobdir / "requests.seen").write_text("frontier")

        run_spider.strike_jobdir(str(jobdir))
        run_spider.clear_jobdir_stall_marker(str(jobdir))

        assert run_spider.strike_jobdir(str(jobdir)) == "marked", (
            "a strike survived a successful open, so two unrelated failures a month apart "
            "would discard a healthy frontier"
        )
        assert (jobdir / "requests.seen").exists()

    def test_a_frontier_that_cannot_be_MARKED_is_discarded_instead(self, tmp_path):
        """The one hole in the bounded-loop argument. If the strike cannot be recorded (full
        or read-only disk) the next run cannot know to stop trusting this frontier, so the loop
        would be unbounded and silent. Failing safe costs a re-crawl; the alternative costs the
        domain permanently."""
        jobdir = tmp_path / "job"
        jobdir.mkdir()
        (jobdir / "requests.seen").write_text("frontier")
        jobdir.chmod(0o500)                      # readable, not writable
        try:
            result = run_spider.strike_jobdir(str(jobdir))
        finally:
            jobdir.chmod(0o700)

        assert result == "discarded", (
            "an unrecordable strike left the frontier in place, so nothing bounds the retry "
            "loop and the domain can fail identically forever with no trace of why"
        )

    def test_striking_a_missing_or_absent_jobdir_does_not_raise(self, tmp_path):
        """Best-effort by design: this runs on the failure path and must never turn a
        reportable failure into a crash."""
        assert run_spider.strike_jobdir(None) == "none"
        assert run_spider.strike_jobdir("") == "none"
        run_spider.strike_jobdir(str(tmp_path / "does-not-exist"))
        run_spider.clear_jobdir_stall_marker(None)
        run_spider.clear_jobdir_stall_marker(str(tmp_path / "does-not-exist"))

    def test_the_marker_is_not_mistaken_for_a_queue_slot(self, tmp_path):
        """`reset_incompatible_jobdir` lists `requests.queue/` and judges the format from its
        entries. The marker lives one level up, in the JOBDIR itself, so the two cannot
        interfere -- but pin it, because a marker written into the queue dir would make every
        resumable crawl look format-incompatible."""
        jobdir = tmp_path / "job"
        (jobdir / "requests.queue").mkdir(parents=True)
        (jobdir / "requests.queue" / "0").mkdir()
        run_spider.strike_jobdir(str(jobdir))

        assert run_spider.reset_incompatible_jobdir(
            str(jobdir), disk_queue="scrapy.squeues.PickleFifoDiskQueue"
        ) is False
        assert jobdir.exists()

    def test_build_settings_does_NOT_discard_a_struck_frontier(self, tmp_path):
        """The production ordering, and the bug this replaced. An earlier draft ran the discard
        as a pre-flight here -- which is BEFORE main() connects `spider_opened`, so a struck
        frontier was deleted before it could ever be cleared and the innocent case still lost
        its crawl. Every test of the clear path passed anyway, because none of them ran the
        pre-flight first."""
        jobdir = tmp_path / "job"
        jobdir.mkdir()
        (jobdir / "requests.seen").write_text("frontier")
        run_spider.strike_jobdir(str(jobdir))

        settings = build_settings(make_args(jobdir=str(jobdir)))

        assert settings["JOBDIR"] == str(jobdir)
        assert (jobdir / "requests.seen").exists(), (
            "build_settings destroyed a struck frontier, so a spider that opens fine can "
            "never clear the strike and #103 preserves nothing"
        )
