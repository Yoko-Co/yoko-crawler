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
from website_spider import WebsiteSpider


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
    args = parser.parse_args()

    # Defense-in-depth: lightweight domain format check.
    try:
        args.domain = validate_domain_format(args.domain)
    except DomainValidationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    process = CrawlerProcess(
        settings={
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
            "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
    )
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
