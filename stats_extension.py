"""
Scrapy extension that writes crawl progress to an atomic status file.

Written every 3 seconds during the crawl and once on spider close.
The FastAPI parent process reads this file to serve GET /crawl/{id}.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from scrapy import signals
from twisted.internet.task import LoopingCall

logger = logging.getLogger(__name__)


class ProgressWriter:
    """Scrapy extension that writes progress to an atomic JSON status file."""

    # Safety-valve close reasons that produce valid (possibly partial) results.
    _COMPLETED_REASONS = {"finished", "closespider_timeout", "closespider_itemcount"}

    def __init__(self, stats, status_file):
        self.stats = stats
        self.status_file = status_file
        self._loop = None

    @classmethod
    def from_crawler(cls, crawler):
        status_file = crawler.settings.get("STATUS_FILE")
        if not status_file:
            raise ValueError(
                "STATUS_FILE setting is required for ProgressWriter extension"
            )
        ext = cls(crawler.stats, status_file)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider):
        self._loop = LoopingCall(self._write_status, "running")
        self._loop.start(3.0)

    def spider_closed(self, spider, reason):
        if self._loop and self._loop.running:
            self._loop.stop()
        status = "completed" if reason in self._COMPLETED_REASONS else "failed"
        error = reason if status == "failed" else None
        # Structured failure discriminator (issue #44): a stable token a consumer
        # (yoko-corpus) switches on instead of scraping `error`/`close_reason` prose.
        # None on a real crawl; set alongside every failed branch below. An abnormal
        # Scrapy close (memusage/OOM/signal) is a generic `crawl_error`; the empty
        # guards below refine it to the specific cause.
        failure_reason = "crawl_error" if status == "failed" else None

        # NOTE: an all-403/blocked crawl (bot-wall) is intentionally NOT failed here. The
        # crawl COMPLETES and emits its 403 rows; the consumer (yoko-corpus) owns the
        # blocked-crawl policy -- it reads the forbidden ratio to retry with browser
        # impersonation and, if still blocked, presents an honest "we couldn't read this
        # site" report. Failing the crawl here would deny the corpus both. The spider's
        # `waf_challenge_count` stat records the wall for observability. (The empty-crawl
        # guards below still fail a crawl that fetched NOTHING -- a genuine empty result.)

        # A crawl that fetched NOTHING gets classified by WHY, so the consumer can tell a
        # mistyped/unreachable target from an SSRF-blocked one instead of a misleading
        # "completed" with zero results. Only ever reclassifies an empty crawl: any
        # crawl that fetched even one page (incl. an all-403 bot-wall) is left completed.
        if status == "completed":
            responses = self.stats.get_value("response_received_count", 0)
            if responses == 0:
                blocked = self.stats.get_value("ssrf_guard/blocked", 0)
                exceptions = self.stats.get_value("downloader/exception_count", 0)
                # Order matters: the SSRF guard drops a host via IgnoreRequest, which Scrapy
                # ALSO counts in downloader/exception_count -- so an all-SSRF-blocked crawl
                # has exceptions>0 too. Checking blocked>0 first keeps it `ssrf_blocked`
                # (the specific cause) rather than the generic `unreachable`.
                if blocked > 0:
                    # Every candidate host resolved to a blocked/reserved range and was
                    # dropped by the SSRF guard.
                    status, failure_reason = "failed", "ssrf_blocked"
                    error = (
                        "crawl blocked by SSRF guard: every target host resolved to "
                        "a private or reserved address; no pages were fetched"
                    )
                elif exceptions > 0:
                    # Every request errored at the transport layer (DNS / connection /
                    # TLS) and nothing was fetched -> the target is unreachable, almost
                    # always a wrong or mistyped address.
                    status, failure_reason = "failed", "unreachable"
                    error = (
                        "target unreachable: every request failed at the network layer "
                        "(DNS or connection) and no pages were fetched -- check the address"
                    )
                # else: 0 responses, no SSRF drops, no transport errors -> a genuinely
                # empty finish (e.g. everything robots-disallowed). Left "completed" as
                # before -- not a new failure mode, so behavior is unchanged.

        # Seeding tripwire (issue #52). Our own seeding method counts every seed it emits,
        # so a crawl that fetched pages while reporting ZERO seeds was seeded by something
        # else -- in practice a framework rename orphaning the method, exactly what
        # silently disabled robots.txt/sitemap discovery for months. Log it loudly: this
        # class of failure produces a plausible-looking crawl (pages ARE fetched, just
        # fewer), so nothing else would ever surface it. Deliberately NOT failed -- a
        # link-followed crawl is degraded, not worthless, and failing it would lose real
        # pages over a defect the operator can fix and re-run.
        if self.stats.get_value("seeding/seeds_emitted", 0) == 0 and \
                self.stats.get_value("response_received_count", 0) > 0:
            logger.error(
                "SEEDING DID NOT RUN: the crawl fetched pages but emitted no seeds of its "
                "own, so robots.txt and sitemap discovery were skipped and this crawl is "
                "link-following only. Almost certainly the Scrapy seeding entry point was "
                "renamed and WebsiteSpider.start() is no longer being called -- check the "
                "installed Scrapy against the pin in requirements.txt."
            )

        # Phase-two seeding tripwire (issue #76). Seeding is no longer one atomic event:
        # the spider seeds robots.txt, and the START URLS are emitted from its callback once
        # the Disallow rules are final. So "seeding ran" is no longer proved by a non-zero
        # seeds_emitted -- phase one can succeed while phase two never happens (an unbounded
        # robots.txt redirect chain, an exception in the callback, a request lost to a
        # middleware that neither calls back nor errbacks). The signature is robots fetched,
        # start URLs never emitted, and the crawl closing "completed" with a one-row
        # inventory -- the most deceptive shape available, so it gets its own loud error.
        if self.stats.get_value("seeding/seeds_emitted", 0) > 0 and \
                self.stats.get_value("seeding/start_urls_emitted", 0) == 0:
            logger.error(
                "SEEDING STOPPED AFTER ROBOTS.TXT: the crawl fetched robots.txt but never "
                "emitted its start URL, so it inventoried nothing. Any page count in this "
                "crawl is robots.txt itself, NOT the site -- do not read it as an inventory. "
                "Usually an unterminated robots.txt redirect chain or an error in "
                "parse_robots; re-run and check the robots.txt of the target."
            )

        self._write_status(
            status, error=error, final=True, close_reason=reason, failure_reason=failure_reason
        )

    def _status_counts(self):
        """HTTP status histogram ({"200": n, "403": n, ...}) from Scrapy's built-in
        `downloader/response_status_count/<code>` stats, so the operator can see the response
        mix (how many 403s, redirects, 404s) at a glance without re-deriving it."""
        prefix = "downloader/response_status_count/"
        counts = {}
        for key, value in self.stats.get_stats().items():
            if key.startswith(prefix):
                counts[key[len(prefix):]] = value
        return counts

    def _write_status(self, status, error=None, final=False, close_reason=None, failure_reason=None):
        data = {
            "status": status,
            "urls_discovered": self.stats.get_value("scheduler/enqueued", 0),
            "urls_crawled": self.stats.get_value("response_received_count", 0),
            "updated_at": time.time(),
            "error": error,
            # The Scrapy close reason, surfaced even on a "completed" close so a
            # consumer can tell a natural `finished` from a safety-valve stop
            # (`closespider_timeout` / `closespider_itemcount`) that produced only
            # partial results. None while the crawl is still running.
            "close_reason": close_reason,
            # Structured failure token (issue #44): None unless the crawl failed with a
            # classified cause (unreachable / ssrf_blocked / crawl_error).
            "failure_reason": failure_reason,
            # Seeding observability (issue #52). `seeds_emitted` is the tripwire for a whole
            # bug CLASS: a framework rename made the spider's seeding method unreachable and
            # Scrapy's default seeded instead, so robots.txt/sitemap discovery silently
            # stopped -- for months, with no exception, no failing test and no log line.
            # A crawl seeded by anything other than our own method reports 0 here.
            # `robots_fetched` separates "the site lists no sitemap" (robots fetched,
            # sitemaps 0) from "we never asked" (robots 0) -- and since #76 a third case,
            # "we asked and the transport failed", which `robots_failed` distinguishes.
            "seeding": {
                "seeds_emitted": self.stats.get_value("seeding/seeds_emitted", 0),
                "robots_fetched": self.stats.get_value("seeding/robots_fetched", 0),
                "sitemaps_fetched": self.stats.get_value("seeding/sitemaps_fetched", 0),
                # Seeding is two-phase since #76 (robots.txt, THEN the start URLs from its
                # callback). A crawl with seeds_emitted > 0 but start_urls_emitted == 0
                # fetched robots.txt and nothing else -- see the tripwire below.
                "start_urls_emitted": self.stats.get_value("seeding/start_urls_emitted", 0),
                # Non-zero means robots.txt could not be FETCHED (DNS/refused/timeout) and
                # the crawl ran allow-all. Without this, `robots_fetched: 0` is ambiguous
                # between "the seeder never ran" and "we asked and the network refused" --
                # and the crawl's robots posture is unrecoverable after the fact.
                "robots_failed": self.stats.get_value("seeding/robots_failed", 0),
            },
            # Block/restriction observability. These are OBSERVED counts, NOT a verdict: on a
            # Cloudflare-fronted site a single 403 can be both a bot challenge and a login page
            # (INCOSE's /setdb-login/ returns `403 cf-mitigated: challenge` to a bot), and the
            # crawler can't recover the origin's intent from one blocked fetch. So it surfaces
            # the raw picture and leaves the blocked-crawl policy to the consumer (yoko-corpus
            # already reads the forbidden ratio; the frontend can show it).
            #   waf_challenge_count    -- responses Cloudflare ITSELF walled (cf-mitigated, or a
            #                             CF fingerprint with no origin headers).
            #   origin_forbidden_count -- 403s the ORIGIN generated (member-restricted content
            #                             we DO want inventoried), kept out of the wall bucket.
            #   status_counts          -- full HTTP status histogram, from Scrapy's own stats.
            "blocking": {
                "waf_challenge_count": self.stats.get_value("waf_challenge_count", 0),
                "origin_forbidden_count": self.stats.get_value("origin_forbidden_count", 0),
                "status_counts": self._status_counts(),
            },
        }
        if final:
            data["finished_at"] = datetime.now(timezone.utc).isoformat()

        # Atomic write: fixed temp path, then rename.
        tmp_path = self.status_file + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.status_file)
        except OSError:
            # Disk full or permissions — LoopingCall survives,
            # monitor task is the backstop for final status.
            pass
