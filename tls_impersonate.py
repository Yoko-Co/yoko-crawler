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

# User-Agent strings matching each pinned target. scrapy-impersonate forwards
# Scrapy's headers to curl_cffi, which does NOT inject the impersonation UA when
# headers are supplied -- so we must advertise a UA that matches the TLS
# fingerprint ourselves (Cloudflare cross-checks UA vs JA3). Keep each entry's
# version in step with CURRENT_TARGETS for the same family.
FAMILY_USER_AGENTS = {
    "chrome": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "firefox": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0"
    ),
    "safari": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
}

# Canonical set of --impersonate / API choices -- single source of truth so the
# CLI (argparse choices) and API (Pydantic Literal) cannot drift. "off" disables
# impersonation; the family names map to CURRENT_TARGETS; "random" rotates.
IMPERSONATE_CHOICES = ("off", *CURRENT_TARGETS.keys(), "random")


def user_agent_for(target):
    """Return a browser UA string matching a curl_cffi target (by family prefix).

    Falls back to the Chrome UA for unrecognized/explicit targets so the request
    still carries a plausible browser UA.
    """
    for family, ua in FAMILY_USER_AGENTS.items():
        if target.startswith(family):
            return ua
    return FAMILY_USER_AGENTS["chrome"]


class ImpersonateMiddleware:
    """Tag each request with a current browser target and a matching User-Agent.

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
        # Advertise a UA matching whichever fingerprint this request uses, so
        # UA and JA3 stay consistent (incl. firefox/safari and per-request
        # "random" rotation). setdefault preserves an explicit --user-agent.
        request.headers.setdefault(
            "User-Agent", user_agent_for(request.meta["impersonate"])
        )
