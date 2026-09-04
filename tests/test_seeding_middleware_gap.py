"""The `robots_failed` seeding path runs NO spider middleware (issue #91 part 2).

Scrapy's `Scraper._scrape` wraps only the RESPONSE branch in `spidermw.scrape_response_async`;
errback output goes straight to `handle_spider_output_async`. `robots_failed` is an errback,
so every request it seeds -- the start URL, sitemap probes -- bypasses DepthMiddleware,
OffsiteMiddleware, UrlLengthMiddleware and anything added later.

That is invisible to the rest of the suite, which never runs a real engine, and it bites
exactly on the black-holed and WAF-fronted hosts robots_failed exists for. #82 pinned
sitemap-probe depth explicitly so the one KNOWN divergence cannot hurt; this pins the general
property so a future middleware cannot be added on the quiet assumption that it applies.

Behavioural probe, not a source grep -- the same shape as `_depth_reset_supported` in
website_spider.py, which instantiates the real middleware and calls it.
"""

import asyncio

import pytest
from scrapy import Request, Spider
from scrapy.core.scraper import Scraper
from scrapy.http import Response
from scrapy.utils.test import get_crawler
from twisted.python.failure import Failure

SEEN: list = []


class _Recorder:
    """A spider middleware that records everything it is given."""

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    async def process_spider_output(self, response, result):
        async for item in result:
            SEEN.append(item)
            yield item


class _Probe(Spider):
    name = "seeding-probe"

    def parse(self, response):
        yield Request("https://example.invalid/from-callback")

    def seed_from_errback(self, failure):
        yield Request("https://example.invalid/from-errback")


async def _drive(make_result, attach):
    SEEN.clear()
    crawler = get_crawler(_Probe, {"SPIDER_MIDDLEWARES": {_Recorder: 543}})
    crawler.spider = crawler._create_spider("seeding-probe")
    scraper = Scraper(crawler)
    await scraper.open_spider_async()
    request = Request("https://example.invalid/seed", **attach(crawler.spider))
    await scraper._scrape(make_result(request), request)
    return [r.url.rsplit("/", 1)[-1] for r in SEEN]


def test_the_callback_path_DOES_run_spider_middleware():
    """The control. Without it, a probe that simply never worked would look like a pass."""
    seen = asyncio.run(_drive(
        lambda req: Response("https://example.invalid/seed", request=req),
        lambda spider: {"callback": spider.parse},
    ))
    assert seen == ["from-callback"]


def test_the_errback_path_runs_NO_spider_middleware():
    """The asymmetry itself. If this ever starts passing through middleware, Scrapy has
    changed where it routes errback output -- which is good news, and means the workarounds
    built around this (the pinned sitemap-probe depth in #82) can be revisited."""
    seen = asyncio.run(_drive(
        lambda req: Failure(ValueError("transport failure")),
        lambda spider: {"errback": spider.seed_from_errback},
    ))
    assert seen == [], (
        "spider middleware now runs on the errback path -- #91 part 2's asymmetry is gone. "
        "Re-check the explicit depth pin in _sitemap_probe_requests and this file's premise."
    )
