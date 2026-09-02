"""Tests for tls_impersonate.ImpersonateMiddleware."""

from types import SimpleNamespace

from tls_impersonate import (
    CURRENT_TARGETS,
    FAMILY_USER_AGENTS,
    ImpersonateMiddleware,
    user_agent_for,
)


def make_request(meta=None, headers=None):
    # headers is a plain dict here; Scrapy's real Headers also supports setdefault.
    return SimpleNamespace(meta=dict(meta or {}), headers=dict(headers or {}))


def make_crawler(setting=None):
    """Fake crawler whose settings.get returns `setting`, or the default when absent."""
    if setting is None:
        get = lambda key, default=None: default  # noqa: E731 - setting absent
    else:
        get = lambda key, default=None: setting  # noqa: E731
    return SimpleNamespace(settings=SimpleNamespace(get=get))


class TestPool:
    def test_family_name_resolves_to_pinned_version(self):
        assert ImpersonateMiddleware("chrome").pool == [CURRENT_TARGETS["chrome"]]
        assert ImpersonateMiddleware("firefox").pool == [CURRENT_TARGETS["firefox"]]
        assert ImpersonateMiddleware("safari").pool == [CURRENT_TARGETS["safari"]]

    def test_random_builds_full_current_pool(self):
        mw = ImpersonateMiddleware("random")
        assert set(mw.pool) == set(CURRENT_TARGETS.values())
        assert len(mw.pool) == len(CURRENT_TARGETS)

    def test_explicit_target_passes_through(self):
        # An explicit curl_cffi target not in CURRENT_TARGETS is forwarded as-is.
        assert ImpersonateMiddleware("chrome146").pool == ["chrome146"]


class TestFromCrawler:
    def test_reads_impersonate_target_setting(self):
        mw = ImpersonateMiddleware.from_crawler(make_crawler("firefox"))
        assert mw.pool == [CURRENT_TARGETS["firefox"]]

    def test_defaults_to_chrome_when_setting_absent(self):
        mw = ImpersonateMiddleware.from_crawler(make_crawler(None))
        assert mw.pool == [CURRENT_TARGETS["chrome"]]


class TestProcessRequest:
    def test_sets_impersonate_when_absent(self):
        req = make_request()
        ImpersonateMiddleware("chrome").process_request(req, spider=None)
        assert req.meta["impersonate"] == CURRENT_TARGETS["chrome"]

    def test_does_not_overwrite_existing_meta(self):
        # setdefault must preserve an explicit per-request target (and a retried
        # request's already-assigned fingerprint).
        req = make_request({"impersonate": "safari180"})
        ImpersonateMiddleware("chrome").process_request(req, spider=None)
        assert req.meta["impersonate"] == "safari180"

    def test_random_assigns_a_target_from_the_pool(self):
        mw = ImpersonateMiddleware("random")
        req = make_request()
        mw.process_request(req, spider=None)
        assert req.meta["impersonate"] in set(CURRENT_TARGETS.values())

    def test_sets_user_agent_matching_the_target(self):
        req = make_request()
        ImpersonateMiddleware("firefox").process_request(req, spider=None)
        assert req.headers["User-Agent"] == FAMILY_USER_AGENTS["firefox"]

    def test_does_not_overwrite_explicit_user_agent(self):
        req = make_request(headers={"User-Agent": "custom-agent"})
        ImpersonateMiddleware("chrome").process_request(req, spider=None)
        assert req.headers["User-Agent"] == "custom-agent"


class TestUserAgentFor:
    def test_resolves_each_family(self):
        assert user_agent_for("chrome131") == FAMILY_USER_AGENTS["chrome"]
        assert user_agent_for("firefox147") == FAMILY_USER_AGENTS["firefox"]
        assert user_agent_for("safari180") == FAMILY_USER_AGENTS["safari"]

    def test_unknown_target_falls_back_to_chrome(self):
        assert user_agent_for("edge101") == FAMILY_USER_AGENTS["chrome"]


class TestDownloadTimeoutForwarding:
    """Issue #88: scrapy-impersonate reads neither `download_timeout` nor DOWNLOAD_TIMEOUT,
    so every impersonated request silently ran on curl_cffi's undeclared session default.

    Asserted through scrapy-impersonate's REAL parser wherever possible -- the whole bug was
    that the value we set was never the value the dependency consumed, so a test that checks
    our own meta would reproduce the bug rather than catch it."""

    def _mw(self):
        return ImpersonateMiddleware("chrome")

    def test_the_undeclared_default_this_replaces_is_real(self):
        """Pin curl_cffi's session default, since the whole finding is that we inherited it.

        The first version of this test read `inspect.getsource(AsyncSession.__init__)` and
        asserted the substring "timeout" -- but `AsyncSession.__init__` takes no `timeout`
        parameter at all (self, loop, async_curl, max_clients, **kwargs), so the only match
        was a word in its DOCSTRING. It passed for a reason unrelated to the default and
        could never have caught the default moving, which is the one thing it existed to do.
        The default lives on `BaseSession`."""
        import inspect
        from curl_cffi.requests.session import AsyncSession, BaseSession
        assert "timeout" not in inspect.signature(AsyncSession.__init__).parameters
        assert inspect.signature(BaseSession.__init__).parameters["timeout"].default == 30

    def test_the_setting_reaches_curl_cffi(self):
        """The end-to-end claim, through the real RequestParser that builds curl's kwargs."""
        from scrapy.http import Request
        from scrapy_impersonate.parser import RequestParser
        req = Request("https://example.com/", meta={"download_timeout": 60})
        self._mw().process_request(req, spider=None)
        assert RequestParser(req).as_dict()["timeout"] == 60

    def test_without_forwarding_curl_gets_no_timeout_at_all(self):
        """The bug, pinned: the same request that has NOT been through this middleware
        produces curl kwargs with no timeout key."""
        from scrapy.http import Request
        from scrapy_impersonate.parser import RequestParser
        req = Request("https://example.com/", meta={"download_timeout": 60})
        assert "timeout" not in RequestParser(req).as_dict()

    def test_an_explicit_impersonate_args_timeout_still_wins(self):
        """A deliberate per-request override must not be clobbered -- this is how the
        robots.txt budget pins its own bound on this path."""
        req = make_request(meta={"download_timeout": 180,
                                 "impersonate_args": {"timeout": 60}})
        self._mw().process_request(req, spider=None)
        assert req.meta["impersonate_args"]["timeout"] == 60

    def test_other_impersonate_args_are_preserved(self):
        req = make_request(meta={"download_timeout": 60,
                                 "impersonate_args": {"params": {"a": "1"}}})
        self._mw().process_request(req, spider=None)
        assert req.meta["impersonate_args"] == {"params": {"a": "1"}, "timeout": 60}

    def test_no_timeout_is_invented_when_scrapy_has_none(self):
        """Mirrors DownloadTimeoutMiddleware's own `if self._timeout:`.

        The first version of this docstring said "DOWNLOAD_TIMEOUT can be set to 0 to disable
        the bound", which is wrong: with 0, Scrapy never sets the meta key and the default
        path lands on a 10s connect timeout, so 0 is not "no limit" there either. The real
        reason not to forward it is that libcurl reads TIMEOUT_MS 0 as INDEFINITE, which
        would hang a downloader slot until CLOSESPIDER_TIMEOUT."""
        for meta in ({}, {"download_timeout": 0}, {"download_timeout": None}):
            req = make_request(meta=meta)
            self._mw().process_request(req, spider=None)
            assert "timeout" not in req.meta.get("impersonate_args", {})

    def test_a_non_dict_impersonate_args_does_not_crash_the_crawl(self):
        req = make_request(meta={"download_timeout": 60, "impersonate_args": "nonsense"})
        self._mw().process_request(req, spider=None)
        assert req.meta["impersonate_args"] == {"timeout": 60}

    def test_the_middleware_ordering_this_relies_on_holds(self):
        """Forwarding only works because DownloadTimeoutMiddleware has already populated the
        meta by the time this middleware runs. Asserted against Scrapy's real priority table
        and the priority run_spider actually registers, not against the numbers in a comment."""
        from scrapy.settings import default_settings as d
        from run_spider import build_settings
        from tests.test_run_spider import make_args
        dt = d.DOWNLOADER_MIDDLEWARES_BASE[
            "scrapy.downloadermiddlewares.downloadtimeout.DownloadTimeoutMiddleware"]
        ours = build_settings(make_args(impersonate="chrome"))[
            "DOWNLOADER_MIDDLEWARES"]["tls_impersonate.ImpersonateMiddleware"]
        assert dt < ours, "DownloadTimeoutMiddleware must run before ImpersonateMiddleware"


class TestForwardingHasNoSharedState:
    """#88 review, raised independently by two reviewers: the forwarder used to MUTATE
    `meta["impersonate_args"]` in place, and `Request.copy()`/`replace()` only shallow-copy
    meta -- so one nested dict was shared by every derived request."""

    def _mw(self):
        return ImpersonateMiddleware("chrome")

    def test_two_requests_derived_from_one_parent_get_their_own_timeouts(self):
        """The aliasing case. Dormant while every impersonated request has the same bound,
        live the moment a derived request is re-timed -- a retry or redirect that should
        carry a different budget would silently inherit the parent's."""
        from scrapy.http import Request
        from scrapy_impersonate.parser import RequestParser
        parent = Request("https://example.com/", meta={"download_timeout": 60})
        self._mw().process_request(parent, spider=None)

        child = parent.replace(url="https://example.com/slow")
        child.meta["download_timeout"] = 5
        child.meta.pop("impersonate_args", None)   # a re-timed derivative
        self._mw().process_request(child, spider=None)

        assert RequestParser(parent).as_dict()["timeout"] == 60
        assert RequestParser(child).as_dict()["timeout"] == 5
        assert parent.meta["impersonate_args"] is not child.meta["impersonate_args"]

    def test_the_forwarder_does_not_alias_a_caller_supplied_dict(self):
        """A caller's own dict must not become the request's live meta object."""
        from scrapy.http import Request
        supplied = {"params": {"a": "1"}}
        req = Request("https://example.com/",
                      meta={"download_timeout": 60, "impersonate_args": supplied})
        self._mw().process_request(req, spider=None)
        assert req.meta["impersonate_args"] is not supplied
        assert "timeout" not in supplied, "the caller's dict was mutated"
        assert req.meta["impersonate_args"]["params"] == {"a": "1"}

    def test_a_retry_copy_keeps_its_bound(self):
        """RetryMiddleware rebuilds the request with `.copy()`; the forwarded value has to
        survive that or attempts after the first silently lose the bound."""
        from scrapy.http import Request
        from scrapy_impersonate.parser import RequestParser
        req = Request("https://example.com/", meta={"download_timeout": 60})
        self._mw().process_request(req, spider=None)
        assert RequestParser(req.copy()).as_dict()["timeout"] == 60

    def test_download_timeout_zero_falls_back_to_curls_default(self):
        """Pins the ONE case #88 does not close, so it is documented rather than discovered:
        with DOWNLOAD_TIMEOUT 0 nothing is forwarded and this path is back on curl_cffi's
        undeclared default. Deliberate -- forwarding 0 means an INDEFINITE libcurl timeout.
        No caller sets 0 and `run_spider` always declares a value on this path."""
        from scrapy.http import Request
        from scrapy_impersonate.parser import RequestParser
        req = Request("https://example.com/", meta={"download_timeout": 0})
        self._mw().process_request(req, spider=None)
        assert "timeout" not in RequestParser(req).as_dict()
