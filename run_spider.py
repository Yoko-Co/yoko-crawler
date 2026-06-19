#!/usr/bin/env python3
"""
Subprocess entry point for running the Scrapy spider.

Invoked by job_manager.py via asyncio.create_subprocess_exec.
Accepts --domain, --output, --status-file as command-line arguments.
"""

import argparse
import json
import sys
import time

from scrapy.crawler import CrawlerProcess

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
    settings = {
        "FEEDS": {
            args.output: {
                "format": args.format,
                "overwrite": True,
            }
        },
        "FEED_EXPORT_FIELDS": [
            "url",
            "status",
            "last_modified",
            "redirected_to",
            "referrer",
        ],
        "USER_AGENT": args.user_agent or DEFAULT_USER_AGENT,
        "CLOSESPIDER_TIMEOUT": 7200,
        "CLOSESPIDER_ITEMCOUNT": 50000,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": args.delay,
        "AUTOTHROTTLE_MAX_DELAY": max(30, args.delay * 10),
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0 if args.delay >= 3 else 2.0,
        "CONCURRENT_REQUESTS": 1 if args.delay >= 3 else 16,
        "DOWNLOAD_DELAY": args.delay,
        "MEMUSAGE_LIMIT_MB": 384,
        "MEMUSAGE_CHECK_INTERVAL_SECONDS": 30,
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
    )
    process.start()


if __name__ == "__main__":
    main()
