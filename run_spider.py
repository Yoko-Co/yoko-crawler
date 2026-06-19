#!/usr/bin/env python3
"""
Subprocess entry point for running the Scrapy spider.

Invoked by job_manager.py via asyncio.create_subprocess_exec.
Accepts --domain, --output, --status-file as command-line arguments.
"""

import argparse
import sys

from scrapy.crawler import CrawlerProcess

from domain_validator import DomainValidationError, validate_domain_format
from stats_extension import ProgressWriter
from tls_impersonate import IMPERSONATE_CHOICES
from website_spider import WebsiteSpider

# Default User-Agent for the non-impersonate path. When --impersonate is set,
# curl_cffi supplies the browser-matching UA instead (see below), so this only
# applies to standard Scrapy crawls.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
        sys.exit(1)

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
    }

    # Browser TLS-fingerprint impersonation (curl_cffi via scrapy-impersonate).
    # Defeats Cloudflare Bot Management and similar WAFs that fingerprint the
    # TLS ClientHello (JA3/JA4) and 403 standard Scrapy regardless of headers.
    if args.impersonate != "off":
        # Fail fast with a clear message if the optional dependency is missing,
        # rather than letting Scrapy raise a cryptic handler-load traceback.
        try:
            import scrapy_impersonate  # noqa: F401
        except ImportError:
            print(
                "--impersonate requires the 'scrapy-impersonate' package "
                "(pip install scrapy-impersonate). Use --impersonate off to "
                "crawl with standard Scrapy TLS.",
                file=sys.stderr,
            )
            sys.exit(2)
        settings.update(
            {
                "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
                "DOWNLOAD_HANDLERS": {
                    "http": "scrapy_impersonate.ImpersonateDownloadHandler",
                    "https": "scrapy_impersonate.ImpersonateDownloadHandler",
                },
                # Our own middleware pins a current, verified browser target.
                # scrapy-impersonate's RandomBrowserMiddleware would rotate into
                # stale fingerprints (chrome99, edge101, ...) that WAFs block.
                "DOWNLOADER_MIDDLEWARES": {
                    "tls_impersonate.ImpersonateMiddleware": 725,
                },
                "IMPERSONATE_TARGET": args.impersonate,
                # Cloudflare still issues occasional bot challenges (403) under
                # concurrency even with a good fingerprint. Retrying lets the
                # __cf_bm cookie set on the challenge carry into the retry, which
                # then passes. Genuinely-restricted pages just exhaust retries.
                "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429, 403],
                # Cap retries at 1: enough to recover a transient challenge via
                # the __cf_bm cookie, without tripling outbound load on a site
                # that 403s broadly (each retry re-hits the same WAF).
                "RETRY_TIMES": 1,
            }
        )
        # NOTE: keep an explicit browser USER_AGENT set while impersonating.
        # scrapy-impersonate forwards Scrapy's header dict to curl_cffi, which
        # does NOT re-add the impersonation UA when headers are supplied -- a
        # blank UA gets the request 403'd. DEFAULT_USER_AGENT tracks the Chrome
        # major version pinned in tls_impersonate.CURRENT_TARGETS so the
        # advertised UA stays consistent with the chrome TLS fingerprint.

    process = CrawlerProcess(settings=settings)
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
