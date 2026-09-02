#!/usr/bin/env python3
"""
Subprocess entry point for running the Scrapy spider.

Invoked by job_manager.py via asyncio.create_subprocess_exec.
Accepts --domain, --output, --status-file as command-line arguments.
"""

import argparse
import json
import os
import shutil
import sys
import time

from scrapy.crawler import CrawlerProcess

from urllib.parse import urlparse

from content_extractor import ENRICHMENT_FIELD_NAMES
from domain_validator import (
    DomainValidationError,
    check_resolution_sync,
    host_resolves_to_blocked,
    validate_domain_format,
)
from stats_extension import ProgressWriter
from tls_impersonate import FAMILY_USER_AGENTS, IMPERSONATE_CHOICES
from website_spider import WebsiteSpider

# Default User-Agent for the non-impersonate path. When --impersonate is set,
# ImpersonateMiddleware sets a per-request UA matching the fingerprint instead.
# Sourced from tls_impersonate so the chrome UA has a single definition.
DEFAULT_USER_AGENT = FAMILY_USER_AGENTS["chrome"]

# NDJSON/CSV columns, in order. The five originals are unchanged for backward
# compatibility; the enrichment columns come from content_extractor's single
# source of truth (ENRICHMENT_FIELD_NAMES). content_text is appended only when
# --emit-content is set.
ORIGINAL_FEED_FIELDS = ["url", "status", "last_modified", "redirected_to", "referrer"]
# `skip_reason` (issue #43): empty on a fetched page, set on a deliberately-skipped URL
# (auth/login-gated) so the corpus can route it to excluded_urls, never page_versions.
BASE_FEED_FIELDS = ORIGINAL_FEED_FIELDS + ["skip_reason"] + list(ENRICHMENT_FIELD_NAMES)

# Bound the download itself so a hostile multi-hundred-MB response can't blow the
# memory cap before our per-body guard runs. Well above any real HTML page.
_DOWNLOAD_MAXSIZE = 64 * 1024 * 1024  # 64 MB
_DOWNLOAD_WARNSIZE = 8 * 1024 * 1024  # 8 MB


def _write_failed_status(status_file, error):
    """Write a terminal 'failed' status so job_manager surfaces ``error`` via the
    API instead of an opaque exit code -- ProgressWriter hasn't started yet when
    a startup validation check fails."""
    try:
        with open(status_file, "w") as f:
            json.dump(
                {
                    "status": "failed",
                    "urls_discovered": 0,
                    "urls_crawled": 0,
                    "updated_at": time.time(),
                    "error": error,
                },
                f,
            )
    except OSError:
        pass


def reset_incompatible_jobdir(jobdir, *, disk_queue: str) -> bool:
    """Discard a JOBDIR whose on-disk frontier was written by a DIFFERENT queue format
    (issue #52). Returns True if it was reset.

    Switching to breadth-first swapped `SCHEDULER_DISK_QUEUE` from Scrapy's default
    `PickleLifoDiskQueue` to `PickleFifoDiskQueue`. queuelib's two implementations write
    incompatible layouts, so resuming a pre-upgrade JOBDIR dies in `Scheduler.open()` with
    `NotADirectoryError` -- and every paused multi-session crawl in flight at rollout is
    exactly that case. `job_manager` only wipes a JOBDIR that closed with no reason; this
    one closes as `shutdown`, so the JOBDIR is KEPT and every later session repeats the
    crash forever. Left alone it permanently bricks the domain.

    Where the formats differ is one level DOWN, which is the trap here: `Scheduler._dqdir`
    mkdirs `requests.queue/` under BOTH formats, so testing that path says nothing. It is
    each per-priority SLOT inside it that differs -- a FILE under Lifo, a DIRECTORY under
    Fifo (verified against the installed queuelib; crossing them raises NotADirectoryError
    one way and IsADirectoryError the other, so a ROLLBACK is equally fatal).

    Comparing the slots against the format we are about to use makes this symmetric: it
    self-heals a rollback as well as a roll-forward. Cost is one restart from the seed per
    in-flight domain, once, with a clear log line.
    """
    queue_dir = os.path.join(str(jobdir), "requests.queue")
    if not os.path.isdir(queue_dir):
        return False  # no persisted frontier yet -- nothing to be incompatible with
    # `active.json` is Scheduler bookkeeping, not a queue slot.
    slots = [os.path.join(queue_dir, e) for e in os.listdir(queue_dir) if e != "active.json"]
    if not slots:
        return False
    expects_directory_slots = "Fifo" in disk_queue
    if all(os.path.isdir(s) == expects_directory_slots for s in slots):
        return False  # on-disk format already matches what we are about to use
    print(
        f"JOBDIR {jobdir} holds a frontier written by the other scheduler queue format, "
        "which cannot be resumed with the current one; discarding it and starting this "
        "domain from the seed. Expected once per in-flight domain when the queue format "
        "changes (issue #52).",
        file=sys.stderr,
    )
    shutil.rmtree(jobdir, ignore_errors=True)
    return True


# Accepted forward-proxy URL schemes -- must match main.py's CrawlRequest validator so a value
# that isn't a real proxy URL can't be smuggled into curl's proxy option as file://, gopher://, etc.
_PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")


def _validate_proxy(proxy: str) -> None:
    """Re-validate the effective proxy at the crawl subprocess's OWN trust boundary -- the single
    point both the API (via the YOKO_CRAWL_PROXY env var) and the CLI (--proxy) funnel through --
    so the scheme allowlist and SSRF host check hold no matter who launched us (issue #22). The
    API validates too, but this guarantees a direct/scripted invocation can't bypass it. Fail
    closed: a requested-but-invalid proxy raises here, aborting the crawl, so we never silently
    fall back to a direct (droplet-IP) fetch when a proxy was asked for."""
    if any(c in proxy for c in "\r\n\x00"):
        raise ValueError("proxy must not contain control characters (CR, LF, or NUL)")
    if not proxy.startswith(_PROXY_SCHEMES):
        raise ValueError(f"proxy scheme must be one of {_PROXY_SCHEMES}")
    host = urlparse(proxy).hostname
    if host and host_resolves_to_blocked(host):
        raise ValueError(
            f"proxy host {host!r} resolves to a private/reserved address -- refusing to route "
            "egress through an internal host"
        )


def build_settings(args):
    """Assemble the Scrapy settings dict for a crawl (pure, so it's testable)."""
    feed_fields = list(BASE_FEED_FIELDS)
    if args.emit_content:
        feed_fields.append("content_text")

    # Crawl profile. "presale" is a politer bundle for sites we don't control
    # (and have permission to crawl): force serial mode with a >=3s delay. It
    # reuses the existing --delay>=3 serial path and never relaxes SSRF/domain
    # validation. "standard" leaves the operator's delay untouched.
    delay = args.delay
    if args.profile == "presale":
        delay = max(delay, 3.0)
    serial = delay >= 3

    settings = {
        "FEEDS": {
            args.output: {
                "format": args.format,
                "overwrite": True,
            }
        },
        "FEED_EXPORT_FIELDS": feed_fields,
        "USER_AGENT": args.user_agent or DEFAULT_USER_AGENT,
        # Cookie jar ON (Scrapy's default, stated explicitly): the site's own session /
        # load-balancer-affinity cookies (e.g. ASP.NET ARRAffinity) persist across the crawl
        # the way a browser keeps them, so requests after the first stay on one backend.
        "COOKIES_ENABLED": True,
        # Breadth-first ordering (issue #52). Scrapy defaults to a LIFO queue -- depth-first
        # -- with no depth limit, which makes an infinitely-branching subtree a TRAPDOOR
        # rather than a tax: the crawler descends into it and never returns, because every
        # page in it pushes more of it onto the stack. On naeyc.org the crawl fetched 430 real
        # pages, hit a faceted-search subtree at row 430, and fetched ZERO real pages
        # afterwards -- the remaining 1,491 requests all went to filter permutations.
        #
        # Under BFO a trap costs a slice of the crawl proportional to its branching and can
        # never monopolize it, because shallow real pages are always served first. This is the
        # GENERAL protection: #49's facet guard closes one trapdoor, but a path-based trap
        # (a calendar walking /events/2027/03/ -> /04/ -> forever) is invisible to any
        # query-param heuristic.
        #
        # The DISK queue only applies when JOBDIR is set (Scheduler.open: `self.dqs = self._dq()
        # if self.dqdir else None`), which job_manager does for resumable crawls. A
        # non-resumable crawl keeps its whole frontier in the memory queue -- and a BFO frontier
        # is wider than a LIFO one -- so MEMUSAGE_LIMIT_MB below is what bounds it there.
        "DEPTH_PRIORITY": 1,
        "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
        "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
        # A session cap, NOT a crawl budget: on either close reason the corpus starts another
        # resumable session against the same JOBDIR (yoko-corpus services/crawl.py), so a site
        # bigger than one session still crawls to completion.
        "CLOSESPIDER_TIMEOUT": 7200,
        "CLOSESPIDER_ITEMCOUNT": 50000,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": delay,
        "AUTOTHROTTLE_MAX_DELAY": max(30, delay * 10),
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0 if serial else 2.0,
        "CONCURRENT_REQUESTS": 1 if serial else 16,
        "DOWNLOAD_DELAY": delay,
        "MEMUSAGE_LIMIT_MB": 384,
        "MEMUSAGE_CHECK_INTERVAL_SECONDS": 30,
        # Drop oversized responses at download time, before the body reaches lxml.
        "DOWNLOAD_MAXSIZE": _DOWNLOAD_MAXSIZE,
        "DOWNLOAD_WARNSIZE": _DOWNLOAD_WARNSIZE,
        "DNSCACHE_ENABLED": True,
        "LOG_LEVEL": "INFO",
        "EXTENSIONS": {ProgressWriter: 500},
        "STATUS_FILE": args.status_file,
        # SSRF connect-time guard: drops any request whose host resolves to a
        # blocked range, before the download handler (default or curl_cffi) runs.
        "DOWNLOADER_MIDDLEWARES": {
            "ssrf_guard.SsrfGuardMiddleware": 90,
        },
    }

    # Forward-proxy egress (issue #22): route every request through the proxy by setting
    # request.meta["proxy"], which both download handlers honor. Registered AFTER the SSRF
    # guard (90) so the guard still resolves + vets the TARGET host first -- the proxy is
    # transport only. The value arrives either on --proxy (local/CLI) or, from the job manager,
    # in the YOKO_CRAWL_PROXY env var (kept off argv so embedded creds aren't world-readable via
    # `ps`). Validate + SSRF-vet it HERE, the one place both paths converge, before installing
    # the middleware; a bad value raises so the subprocess exits rather than crawling direct.
    # Absent a proxy -> ProxyMiddleware NotConfigured, byte-identical crawl.
    proxy = getattr(args, "proxy", None) or os.environ.get("YOKO_CRAWL_PROXY")
    if proxy:
        _validate_proxy(proxy)
        settings["YOKO_CRAWL_PROXY"] = proxy
        settings["DOWNLOADER_MIDDLEWARES"]["proxy_middleware.ProxyMiddleware"] = 100

    # Resumable crawl: Scrapy persists the request frontier + dupefilter to JOBDIR and,
    # on a re-launch with the same dir, resumes -- skipping already-seen URLs and
    # continuing the pending frontier -- instead of re-crawling from the seed (Phase C).
    if getattr(args, "jobdir", None):
        reset_incompatible_jobdir(args.jobdir, disk_queue=settings["SCHEDULER_DISK_QUEUE"])
        settings["JOBDIR"] = args.jobdir

    if args.impersonate == "off":
        return settings

    # Browser TLS-fingerprint impersonation (curl_cffi via scrapy-impersonate).
    # Presents a current browser's TLS ClientHello (JA3/JA4) plus a matching UA, because
    # many CDNs 403 the default Scrapy/curl fingerprint OUTRIGHT -- regardless of intent or
    # headers -- and would refuse even content the origin serves to every visitor. This is a
    # compatibility measure, not a way to override a site's stated "no": we obey robots
    # directives, rel=nofollow, and <meta name=robots>, and we do NOT try to punch through an
    # active Cloudflare challenge (see RETRY_HTTP_CODES below).
    # Fail fast with a clear message if the optional dependency is missing.
    try:
        import scrapy_impersonate  # noqa: F401
    except ImportError:
        msg = (
            "--impersonate requires the 'scrapy-impersonate' package "
            "(pip install scrapy-impersonate). Use --impersonate off to "
            "crawl with standard Scrapy TLS."
        )
        print(msg, file=sys.stderr)
        _write_failed_status(args.status_file, msg)
        sys.exit(2)

    # Our own middleware pins a current, verified browser target.
    # scrapy-impersonate's RandomBrowserMiddleware would rotate into stale
    # fingerprints (chrome99, edge101, ...) that WAFs block. Add it to the
    # existing middleware dict so the SSRF guard above stays registered.
    settings["DOWNLOADER_MIDDLEWARES"]["tls_impersonate.ImpersonateMiddleware"] = 725
    settings.update(
        {
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
            "DOWNLOAD_HANDLERS": {
                "http": "scrapy_impersonate.ImpersonateDownloadHandler",
                "https": "scrapy_impersonate.ImpersonateDownloadHandler",
            },
            "IMPERSONATE_TARGET": args.impersonate,
            # 403 is deliberately NOT retried. A Cloudflare 403 is a challenge/block, and
            # in practice punching through it never worked (the block is driven by IP
            # reputation, not the fingerprint) -- retrying just re-hit the WAF and doubled
            # outbound load on an already-blocked site. Recording the 403 once, immediately,
            # is both the honest signal and what feeds the crawl's block-legibility counts
            # (waf_challenge_count). Transient server/network codes are still retried once.
            "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429],
            # Cap retries at 1: enough to recover a genuinely transient blip without
            # tripling outbound load on a site that errors broadly.
            "RETRY_TIMES": 1,
            # Per-request timeout for impersonated crawls, declared HERE rather than
            # inherited (issue #88). scrapy-impersonate forwarded no timeout to curl_cffi at
            # all, so this path silently ran on curl_cffi's session default -- measured at
            # 30.0s -- a bound nobody chose, invisible in this file, and free to move when
            # curl_cffi changes it. `ImpersonateMiddleware` now forwards `download_timeout`,
            # so this setting is what actually applies.
            #
            # 60s is a JUDGEMENT CALL, and the honest version of the reasoning is that it
            # has no measured backing. There is no WAF page-latency data anywhere in this
            # repo; 60s is curl_cffi's 30s doubled, and it matches the robots budget's
            # DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT -- a coincidence that YOKO_CRAWL_ROBOTS_TIMEOUT
            # (#92) can now end, since it raises the robots side only,
            # which was itself sized for a 1KB text file rather than a page. #82 deferred
            # this exact call to #88 and #88 is answering it by analogy. Revisit it the first
            # time a real crawl produces `unreachable.timeout` rows for pages that later
            # fetch fine -- that is the evidence nobody has yet.
            #
            # The two neighbours it sits between:
            #  - NOT Scrapy's 180s. `RETRY_TIMES: 1` above means the real cost of a hung URL
            #    is (retries + 1) x timeout, so 180s is 360s per URL -- 5% of a 7200s session
            #    -- against 120s (1.7%) at 60s. That bites hardest at CONCURRENT_REQUESTS 1,
            #    where one hung request stalls the whole crawl. (An earlier version of this
            #    comment quoted 2.5%/0.8% by forgetting the retry, understating both by half.)
            #  - NOT the old 30s: inside the range a slow-but-real page can take, so the
            #    undeclared bound was recording real pages as transport failures.
            #
            # One caveat on the first bullet: `--impersonate` and `--profile presale` are
            # INDEPENDENT flags, in argparse and in the API, so the CONCURRENT_REQUESTS 1
            # case is a convention rather than a guarantee -- `{"impersonate": "chrome"}`
            # with no profile runs 16-wide, where a hung slot costs far less. The pairing is
            # the common case and the worst case, which is what a bound should be sized for.
            #
            # AND THE PER-PAGE COST IS NOT THE BOUND. Raising 30s -> 60s is precisely what
            # lets a page answering in 30-60s SUCCEED, and a successful slow response feeds
            # AUTOTHROTTLE a latency sample, which backs the slot off toward
            # AUTOTHROTTLE_MAX_DELAY (30s above). Measured: a server answering a real 200
            # after 40s gives download_latency 40.0 and slot.delay 30.0, so the real cost on
            # such a site is ~90s per page, not 60s. At the old 30s bound those responses
            # timed out, produced no latency sample, and AutoThrottle never moved.
            #
            # That backoff is AutoThrottle working as intended -- a server taking 40s is
            # struggling and slowing down is the polite response, which is the whole point of
            # the presale profile -- so it is not tuned away here. But the alternative it
            # replaces is worse, not cheaper: at 30s those pages were recorded as
            # `unreachable.timeout`, which is a real page reported to a client as a dead one.
            # A slower honest crawl beats a fast wrong inventory. Noting it because the
            # budget arithmetic above prices the bound, and the bound is not the whole cost.
            "DOWNLOAD_TIMEOUT": 60,
        }
    )
    # Let ImpersonateMiddleware set a per-request UA matching each fingerprint
    # (chrome/firefox/safari, incl. "random"). Unset the global USER_AGENT so
    # Scrapy's UserAgentMiddleware doesn't stamp one family's UA onto every
    # request; an explicit --user-agent still overrides. (A blank UA with no
    # middleware-supplied UA gets 403'd -- the middleware guarantees a match.)
    if not args.user_agent:
        settings["USER_AGENT"] = None

    return settings


def main():
    parser = argparse.ArgumentParser(description="Run the website spider")
    parser.add_argument("--domain", required=True, help="Domain to crawl")
    parser.add_argument("--output", required=True, help="Path for JSONL output")
    parser.add_argument(
        "--jobdir",
        default=None,
        help=(
            "Persistent Scrapy JOBDIR for a resumable crawl. When set, the request "
            "frontier + dupefilter persist here and a re-launch resumes instead of "
            "re-crawling from the seed."
        ),
    )
    parser.add_argument(
        "--status-file", required=True, help="Path for status JSON file"
    )
    parser.add_argument(
        "--format",
        choices=["jsonlines", "csv"],
        default="jsonlines",
        help="Output format (default: jsonlines)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1,
        help="Minimum seconds between requests (default: 1, try 3-5 for aggressive WAFs)",
    )
    parser.add_argument(
        "--user-agent",
        default=None,
        help=(
            "User-Agent header sent with every request. Defaults to a current "
            "Chrome UA for standard crawls. When --impersonate is set, leave this "
            "unset so the impersonated browser supplies a matching UA; pass it "
            "only to deliberately override that."
        ),
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help=(
            "Route every request through this forward proxy (issue #22): an "
            "http(s):// CONNECT proxy or a socks5:// proxy, optionally with "
            "user:pass@ auth. The trusted residential-IP egress used only on a "
            "bot-block retry. The SSRF guard still vets the target host."
        ),
    )
    parser.add_argument(
        "--impersonate",
        choices=list(IMPERSONATE_CHOICES),
        default="off",
        help=(
            "Impersonate a real browser's TLS fingerprint (JA3/JA4) via curl_cffi. "
            "Needed for sites behind Cloudflare Bot Management and similar, which "
            "block on the TLS handshake regardless of User-Agent. Default: off "
            "(standard Scrapy TLS). Use 'chrome' for Cloudflare-protected sites."
        ),
    )
    parser.add_argument(
        "--emit-content",
        action="store_true",
        help=(
            "Include the extracted main-content text of each HTML page in a "
            "content_text field. Off by default to keep output lean; the content "
            "hash and all structural counts are emitted regardless. Used by "
            "yoko-corpus when building/refreshing the content store."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=["standard", "presale"],
        default="standard",
        help=(
            "Crawl profile. 'standard' (default) uses the configured delay. "
            "'presale' is a politer bundle for prospect sites we don't control: "
            "serial mode with a >=3s delay. Permission to crawl is an "
            "operational matter handled outside this code."
        ),
    )
    args = parser.parse_args()

    # Defense-in-depth: lightweight domain format check.
    try:
        args.domain = validate_domain_format(args.domain)
    except DomainValidationError as exc:
        print(str(exc), file=sys.stderr)
        _write_failed_status(args.status_file, str(exc))
        sys.exit(1)

    # Re-validate DNS at crawl time (SSRF): the API checked at submit time, but
    # DNS can change before the worker runs. Reject a domain that now resolves to
    # a private/reserved address. SsrfGuardMiddleware re-checks every host below.
    try:
        check_resolution_sync(args.domain)
    except DomainValidationError as exc:
        print(str(exc), file=sys.stderr)
        _write_failed_status(args.status_file, str(exc))
        sys.exit(1)

    process = CrawlerProcess(settings=build_settings(args))
    process.crawl(
        WebsiteSpider,
        domain=args.domain,
        reach_pagination=1,
        include_subdomains=0,
        keep_pagination=0,
        emit_content=1 if args.emit_content else 0,
        output_format=args.format,
    )
    process.start()


if __name__ == "__main__":
    main()
