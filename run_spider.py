#!/usr/bin/env python3
"""
Subprocess entry point for running the Scrapy spider.

Invoked by job_manager.py via asyncio.create_subprocess_exec.
Accepts --domain, --output, --status-file as command-line arguments.
"""

import argparse
import json
import os
import sys
import time

from scrapy.crawler import CrawlerProcess

from content_extractor import ENRICHMENT_FIELD_NAMES
from domain_validator import (
    DomainValidationError,
    check_resolution_sync,
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
BASE_FEED_FIELDS = ORIGINAL_FEED_FIELDS + list(ENRICHMENT_FIELD_NAMES)

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
        # Cookie jar ON (Scrapy's default) -- stated explicitly because injected cookies
        # (--cookies, e.g. a browser-solved cf_clearance) rely on it: the spider seeds the
        # jar on the start request and CookiesMiddleware re-attaches to every followed
        # request to the same domain.
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
        # query-param heuristic. FIFO disk queue keeps a big frontier off the heap.
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

    # Resumable crawl: Scrapy persists the request frontier + dupefilter to JOBDIR and,
    # on a re-launch with the same dir, resumes -- skipping already-seen URLs and
    # continuing the pending frontier -- instead of re-crawling from the seed (Phase C).
    if getattr(args, "jobdir", None):
        settings["JOBDIR"] = args.jobdir

    if args.impersonate == "off":
        return settings

    # Browser TLS-fingerprint impersonation (curl_cffi via scrapy-impersonate).
    # Defeats Cloudflare Bot Management and similar WAFs that fingerprint the
    # TLS ClientHello (JA3/JA4) and 403 standard Scrapy regardless of headers.
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
            # Cloudflare still issues occasional bot challenges (403) under
            # concurrency even with a good fingerprint. Retrying lets the
            # __cf_bm cookie set on the challenge carry into the retry, which
            # then passes. Genuinely-restricted pages just exhaust retries.
            "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429, 403],
            # Cap retries at 1: enough to recover a transient challenge via the
            # __cf_bm cookie, without tripling outbound load on a site that 403s
            # broadly (each retry re-hits the same WAF).
            "RETRY_TIMES": 1,
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
        "--cookies",
        default=None,
        help=(
            "Raw Cookie-header string ('cf_clearance=...; __cf_bm=...') sent with every "
            "request via Scrapy's cookie jar. Use to reuse a browser-solved Cloudflare "
            "clearance cookie. Pair with a matching --user-agent: cf_clearance is bound to "
            "the User-Agent (and usually the IP) that solved the challenge. Prefer the "
            "YOKO_CRAWL_COOKIES env var over this flag for a real (secret) cookie -- an "
            "argv value is world-readable via the process table; the API uses the env var."
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

    # The cookie is a secret (a browser-solved cf_clearance), so the API/job manager passes
    # it via the YOKO_CRAWL_COOKIES env var (readable only by the same uid) rather than argv
    # (world-readable via the process table). The --cookies flag stays for manual/dev use;
    # the env var wins when both are set.
    cookies = os.environ.get("YOKO_CRAWL_COOKIES") or args.cookies

    process = CrawlerProcess(settings=build_settings(args))
    process.crawl(
        WebsiteSpider,
        domain=args.domain,
        reach_pagination=1,
        include_subdomains=0,
        keep_pagination=0,
        emit_content=1 if args.emit_content else 0,
        output_format=args.format,
        cookies=cookies,
    )
    process.start()


if __name__ == "__main__":
    main()
