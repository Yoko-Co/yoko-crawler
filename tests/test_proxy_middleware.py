"""Tests for the egress ProxyMiddleware (issue #22)."""

import types

import pytest
from scrapy.exceptions import NotConfigured
from scrapy.http import Request

from proxy_middleware import ProxyMiddleware


def _crawler(proxy):
    settings = types.SimpleNamespace(get=lambda k, d=None: {"YOKO_CRAWL_PROXY": proxy}.get(k, d))
    return types.SimpleNamespace(settings=settings)


def test_from_crawler_not_configured_without_proxy():
    with pytest.raises(NotConfigured):
        ProxyMiddleware.from_crawler(_crawler(None))
    with pytest.raises(NotConfigured):
        ProxyMiddleware.from_crawler(_crawler(""))


def test_sets_proxy_meta_on_every_request():
    mw = ProxyMiddleware.from_crawler(_crawler("http://user:pass@box:8080"))
    req = Request("https://example.com/a")
    assert mw.process_request(req, spider=None) is None
    assert req.meta["proxy"] == "http://user:pass@box:8080"


def test_does_not_override_an_explicit_request_proxy():
    mw = ProxyMiddleware("http://box:8080")
    req = Request("https://example.com/a", meta={"proxy": "http://other:9"})
    mw.process_request(req, spider=None)
    assert req.meta["proxy"] == "http://other:9"  # setdefault: explicit wins
