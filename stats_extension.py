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

    def __init__(self, stats, status_file, impersonate=None):
        self.stats = stats
        self.status_file = status_file
        # The impersonation target (None when not impersonating). Used to detect
        # an all-blocked crawl whose pinned TLS fingerprint has gone stale.
        self.impersonate = impersonate
        self._loop = None

    @classmethod
    def from_crawler(cls, crawler):
        status_file = crawler.settings.get("STATUS_FILE")
        if not status_file:
            raise ValueError(
                "STATUS_FILE setting is required for ProgressWriter extension"
            )
        ext = cls(
            crawler.stats,
            status_file,
            impersonate=crawler.settings.get("IMPERSONATE_TARGET"),
        )
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

        # Stale-fingerprint guard: an impersonated crawl that made requests but
        # got zero successful (2xx) responses was almost certainly blocked
        # wholesale (the pinned TLS fingerprint aged out of the WAF's allow-list).
        # Surface that as a failure instead of a clean "completed" with no usable
        # results, so the caller knows to bump the targets.
        if status == "completed" and self.impersonate:
            responses = self.stats.get_value("response_received_count", 0)
            ok = self.stats.get_value("downloader/response_status_count/200", 0)
            if responses > 0 and ok == 0:
                status = "failed"
                error = (
                    "impersonated crawl received no successful responses — the "
                    "pinned TLS fingerprint may be stale or the site blocked it"
                )

        self._write_status(status, error=error, final=True)

    def _write_status(self, status, error=None, final=False):
        data = {
            "status": status,
            "urls_discovered": self.stats.get_value("scheduler/enqueued", 0),
            "urls_crawled": self.stats.get_value("response_received_count", 0),
            "updated_at": time.time(),
            "error": error,
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
