"""Downloader middleware that tags every request with a *current* browser TLS
fingerprint so scrapy-impersonate routes it through curl_cffi.

Why not scrapy-impersonate's bundled RandomBrowserMiddleware? It rotates
uniformly across every target curl_cffi ships -- including stale ones
(chrome99..chrome110, edge99/edge101) whose TLS fingerprints modern WAFs like
Cloudflare Bot Management now block. That makes crawls flaky: a recent draw
returns 200, an old draw returns 403. This middleware instead pins a single
current, verified-good target per browser family, so impersonated crawls are
deterministic.

Targets below were verified against Cloudflare Bot Management (napaba.org,
2026-06) with curl_cffi 0.15. Bump them as curl_cffi ships newer browsers.
"""

import random

CURRENT_TARGETS = {
    "chrome": "chrome131",
    "firefox": "firefox147",
    "safari": "safari180",
}

# Canonical set of --impersonate / API choices -- single source of truth so the
# CLI (argparse choices) and API (Pydantic Literal) cannot drift. "off" disables
# impersonation; the family names map to CURRENT_TARGETS; "random" rotates.
IMPERSONATE_CHOICES = ("off", *CURRENT_TARGETS.keys(), "random")


class ImpersonateMiddleware:
    """Set request.meta['impersonate'] to a current browser target.

    Configured via the IMPERSONATE_TARGET setting: a browser family name
    ("chrome"/"firefox"/"safari"), "random" to rotate across the current set,
    or an explicit curl_cffi target string (e.g. "chrome146").
    """

    def __init__(self, target):
        if target == "random":
            self.pool = list(CURRENT_TARGETS.values())
        else:
            # Map a family name to its pinned version; pass through an explicit
            # curl_cffi target unchanged.
            self.pool = [CURRENT_TARGETS.get(target, target)]

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings.get("IMPERSONATE_TARGET", "chrome"))

    def process_request(self, request, spider):
        # setdefault so an explicit per-request meta override still wins.
        request.meta.setdefault("impersonate", random.choice(self.pool))
