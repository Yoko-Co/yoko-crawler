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
        self._forward_download_timeout(request)
        # Advertise a UA matching whichever fingerprint this request uses, so
        # UA and JA3 stay consistent (incl. firefox/safari and per-request
        # "random" rotation). setdefault preserves an explicit --user-agent.
        request.headers.setdefault(
            "User-Agent", user_agent_for(request.meta["impersonate"])
        )

    @staticmethod
    def _forward_download_timeout(request):
        """Carry Scrapy's timeout across to curl_cffi (issue #88).

        scrapy-impersonate builds curl_cffi's kwargs from
        method/url/params/data/headers/cookies/allow_redirects/proxy/impersonate and reads
        NEITHER `download_timeout` NOR the DOWNLOAD_TIMEOUT setting, so it called
        `AsyncSession.request()` with no timeout at all and silently inherited curl_cffi's
        session default -- measured at 30.0s against a socket that accepts and never
        answers. That is a bound nobody in this repo chose, invisible in `run_spider.py`,
        and it MOVES if curl_cffi changes its default.

        30s is also inside the range a slow-but-real page can take, and impersonation is
        used for exactly the WAF-fronted, slow-to-answer sites where that is most likely --
        so the undeclared bound was recording real pages as transport failures on the one
        path least able to afford it.

        `DownloadTimeoutMiddleware` runs at priority 350 and this middleware at 725, so by
        the time we see the request `meta["download_timeout"]` is already populated from the
        setting (or from an explicit per-request value, which is how the robots.txt budget in
        `website_spider._robots_budget_meta` reaches this path). Forwarding it here means one
        timeout mechanism for both download paths instead of two, and per-request overrides
        keep working without either side knowing about the other.

        NOTE what curl's scalar `timeout` actually bounds: curl_cffi maps it to
        CURLOPT_TIMEOUT_MS, a hard cap on the WHOLE transfer, not time-to-first-byte and not
        an idle timeout. With DOWNLOAD_MAXSIZE at 64MB a 60s cap implies >1.1 MB/s sustained
        for a maximal body, and under `--proxy` the CONNECT comes out of the same budget.
        That is not a regression -- the inherited 30s was the same kind of cap, and Scrapy's
        own path enforces a total too -- but the number should be read as a transfer budget,
        not a latency allowance.
        """
        timeout = request.meta.get("download_timeout")
        if not timeout:
            # Mirrors DownloadTimeoutMiddleware's own `if self._timeout:`. Note what this
            # does NOT do: with DOWNLOAD_TIMEOUT 0 nothing is forwarded and this path falls
            # back to curl_cffi's default, i.e. the undeclared ceiling #88 exists to remove,
            # in the one case meant to remove it. Left as-is deliberately rather than
            # forwarding 0 (libcurl reads TIMEOUT_MS 0 as INDEFINITE, which would hang a
            # slot until CLOSESPIDER_TIMEOUT), and Scrapy's own 0 is not "no limit" either
            # -- it skips the meta key and the default path lands on a 10s connect timeout.
            # No caller sets 0, and `run_spider` always declares a value on this path.
            # `test_download_timeout_zero_falls_back_to_curls_default` pins the real
            # behaviour so it is documented rather than discovered.
            return
        args = request.meta.get("impersonate_args")
        # REBUILD rather than mutate in place. `Request.copy()`/`replace()` shallow-copy
        # meta, so mutating the nested dict installs one object shared by every derived
        # request -- a retry, a redirect, anything built from this one. Dormant today
        # (every impersonated request gets the same value, and `_robots_budget_meta()`
        # builds a fresh dict per call), live the moment a derived request is re-timed.
        args = dict(args) if isinstance(args, dict) else {}
        # An explicit `impersonate_args["timeout"]` is a deliberate override and still wins;
        # that is how the robots.txt budget pins its own bound on this path.
        args.setdefault("timeout", timeout)
        request.meta["impersonate_args"] = args
