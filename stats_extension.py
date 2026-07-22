"""
Scrapy extension that writes crawl progress to an atomic status file.

Written every 3 seconds during the crawl and once on spider close.
The FastAPI parent process reads this file to serve GET /crawl/{id}.
"""

import json
import os
import time
from datetime import datetime, timezone

from scrapy import signals
from twisted.internet.task import LoopingCall


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

        self._write_status(
            status, error=error, final=True, close_reason=reason, failure_reason=failure_reason
        )

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
