"""Tests for tls_impersonate.ImpersonateMiddleware."""

from types import SimpleNamespace

from tls_impersonate import CURRENT_TARGETS, ImpersonateMiddleware


def make_request(meta=None):
    return SimpleNamespace(meta=dict(meta or {}))


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
