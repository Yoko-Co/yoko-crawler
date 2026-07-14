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

        # NOTE: an all-403/blocked crawl (bot-wall) is intentionally NOT failed here. The
        # crawl COMPLETES and emits its 403 rows; the consumer (yoko-corpus) owns the
        # blocked-crawl policy -- it reads the forbidden ratio to retry with browser
        # impersonation and, if still blocked, presents an honest "we couldn't read this
        # site" report. Failing the crawl here would deny the corpus both. The spider's
        # `waf_challenge_count` stat records the wall for observability. (The SSRF guard
        # below still fails a crawl that fetched NOTHING -- a genuine empty result.)

        # SSRF guard produced no fetchable pages: every candidate host resolved
        # to a blocked range and was dropped, so the crawl ends empty. Surface as
        # failed rather than a clean "completed" with zero results. (A crawl that
        # fetched pages but dropped a stray internal link has responses>0 and is
        # left as completed -- the SSRF attempt was blocked and the crawl is fine.)
        if status == "completed":
            blocked = self.stats.get_value("ssrf_guard/blocked", 0)
            responses = self.stats.get_value("response_received_count", 0)
            if blocked > 0 and responses == 0:
                status = "failed"
                error = (
                    "crawl blocked by SSRF guard: every target host resolved to "
                    "a private or reserved address; no pages were fetched"
                )

        self._write_status(status, error=error, final=True, close_reason=reason)

    def _write_status(self, status, error=None, final=False, close_reason=None):
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
