"""The `robots_failed` seeding path runs NO spider middleware (issue #91 part 2).

Scrapy's `Scraper._scrape` wraps only the RESPONSE branch in `spidermw.scrape_response_async`;
errback output goes straight to `handle_spider_output_async`. `robots_failed` is an errback,
so every request it seeds -- the start URL, sitemap probes -- bypasses EVERY spider
middleware. This project overrides none, so that is all of SPIDER_MIDDLEWARES_BASE, pinned
below against the live settings rather than restated here. (NOT OffsiteMiddleware, which has
been a DOWNLOADER middleware since Scrapy 2.11 and still filters these requests.)

That is invisible to the rest of the suite, which never runs a real engine, and it bites
exactly on the black-holed and WAF-fronted hosts robots_failed exists for. The sitemap probes pin `depth`
explicitly in their meta so the one KNOWN divergence cannot hurt; this pins the general
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


class _StubEngine:
    """Records what the Scraper schedules. `handle_spider_output_async` hands each yielded
    Request to `crawler.engine.crawl`, so this is where seeded requests land regardless of
    which branch produced them."""

    def __init__(self):
        self.scheduled = []

    def crawl(self, request):
        self.scheduled.append(request)


async def _drive(make_result, attach):
    SEEN.clear()
    crawler = get_crawler(_Probe, {"SPIDER_MIDDLEWARES": {_Recorder: 543}})
    crawler.spider = crawler._create_spider("seeding-probe")
    engine = _StubEngine()
    crawler.engine = engine
    scraper = Scraper(crawler)
    await scraper.open_spider_async()
    request = Request("https://example.invalid/seed", **attach(crawler.spider))
    await scraper._scrape(make_result(request), request)
    tail = lambda reqs: [r.url.rsplit("/", 1)[-1] for r in reqs]
    return tail(SEEN), tail(engine.scheduled)


def test_the_callback_path_DOES_run_spider_middleware():
    """The control. Without it, a probe that simply never worked would look like a pass."""
    seen, scheduled = asyncio.run(_drive(
        lambda req: Response("https://example.invalid/seed", request=req),
        lambda spider: {"callback": spider.parse},
    ))
    assert scheduled == ["from-callback"], "the callback path seeded nothing to compare against"
    assert seen == ["from-callback"]


def test_the_errback_path_runs_NO_spider_middleware():
    """The asymmetry itself. If this ever starts passing through middleware, Scrapy has
    changed where it routes errback output -- which is good news, and means the workarounds
    built around this (the pinned sitemap-probe depth in #82) can be revisited."""
    seen, scheduled = asyncio.run(_drive(
        lambda req: Failure(ValueError("transport failure")),
        lambda spider: {"errback": spider.seed_from_errback},
    ))
    # Absence alone is not evidence: an errback that never ran would also record nothing.
    # This is what makes the empty middleware list mean something.
    assert scheduled == ["from-errback"], (
        "the errback never seeded its request, so the empty middleware list below proves "
        "nothing about the errback PATH -- it just means nothing happened at all"
    )
    assert seen == [], (
        "spider middleware now runs on the errback path -- #91 part 2's asymmetry is gone. "
        "Re-check the explicit depth pin in _sitemap_probe_requests and this file's premise."
    )


# The names AGENTS.md documents as bypassed. Not a restatement of Scrapy's defaults -- a
# tripwire on them, because this list has already drifted twice: it once named
# OffsiteMiddleware (a DOWNLOADER middleware, never bypassed) and once omitted
# MetaCopyDetectionMiddleware.
_DOCUMENTED_BYPASSED = {
    "StartSpiderMiddleware",
    "HttpErrorMiddleware",
    "RefererMiddleware",
    "UrlLengthMiddleware",
    "DepthMiddleware",
    "MetaCopyDetectionMiddleware",
}


def test_the_documented_bypassed_middleware_list_is_complete():
    """AGENTS.md enumerates what the errback path skips. A Scrapy upgrade that adds a spider
    middleware silently widens that blast radius while the documentation still reads as
    exhaustive -- and the enumeration is the only place a reader learns what is NOT protecting
    the seeding path."""
    crawler = get_crawler(_Probe)
    live = {path.rsplit(".", 1)[-1]
            for path in crawler.settings.getwithbase("SPIDER_MIDDLEWARES")}

    assert live == _DOCUMENTED_BYPASSED, (
        "the set of spider middleware this project runs no longer matches what AGENTS.md and "
        "this file document as bypassed on the robots_failed path.\n"
        f"  added since documented:   {sorted(live - _DOCUMENTED_BYPASSED)}\n"
        f"  documented but not live:  {sorted(_DOCUMENTED_BYPASSED - live)}\n"
        "Update both the docstring above and the AGENTS.md JOBDIR/seeding notes."
    )
