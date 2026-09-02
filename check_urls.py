#!/usr/bin/env python3
"""Bulk URL status checker that defeats Cloudflare Bot Management.

Reads a list of URLs (one per line) and writes a CSV reporting each URL's HTTP
status and, for redirects, where it points. Uses the same curl_cffi browser
TLS-fingerprint impersonation the crawler relies on (chrome131 + matching UA),
so it gets past the aggressive Cloudflare WAF that 403s ordinary tools.

Redirects are NOT followed: a 301/302 is reported as such with its Location,
which is exactly what you want when auditing whether URLs 404 or redirect.

Usage:
    python3 check_urls.py urls.txt
    python3 check_urls.py urls.txt -o report.csv --delay 0.5
    python3 check_urls.py urls.txt --impersonate firefox --method GET

Blank lines and lines starting with '#' in the input file are ignored.
"""

import argparse
import csv
import sys
import time

from curl_cffi import requests

# Single source of truth for the verified-good fingerprints + matching UAs,
# shared with the Scrapy crawler so a bump there carries over here.
from tls_impersonate import CURRENT_TARGETS, user_agent_for


def read_urls(path):
    """Yield non-blank, non-comment URLs from a text file (one per line)."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            yield url


# Cloudflare returns these on a clientless/bot-flagged request, but also sets a
# __cf_bm cookie on that same response. Retrying on the shared session (which now
# carries the cookie) clears it. __cf_bm is per-host, so the first hit to each
# new domain in the list trips this once, then passes.
BLOCK_STATUSES = frozenset({403, 429, 503})

# Default pause before a block/exception retry. Applied to BOTH the block-status
# and the raised-exception paths so a raised-then-retried request isn't fired
# back-to-back with zero spacing.
_RETRY_BACKOFF_SECONDS = 0.5
# Cap an honored Retry-After so a hostile/misconfigured host can't stall the run.
_MAX_RETRY_AFTER_SECONDS = 30


def _retry_after(resp):
    """Seconds to wait before retrying a blocked response: the server's
    Retry-After when it's a plain integer (capped), else the default backoff.
    HTTP-date Retry-After is not parsed — not worth it for a one-shot audit; the
    default backoff covers the __cf_bm cookie-warm case."""
    raw = resp.headers.get("Retry-After", "")
    if raw.strip().isdigit():
        return min(int(raw), _MAX_RETRY_AFTER_SECONDS)
    return _RETRY_BACKOFF_SECONDS


def _csv_safe(value):
    """Neutralize spreadsheet formula injection. Excel/LibreOffice treat a cell
    beginning with = + - @ (or a leading tab/CR) as a formula, so a probed host
    can return a Location header like `=cmd|'/c calc'!A1` that executes when the
    operator opens the report. Prefix any such cell with an apostrophe. Mirrors
    the corpus/discovery CSV-export hardening."""
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def check(session, url, target, ua, method, timeout, retries):
    """Probe one URL without following redirects, retrying past CF blocks.

    Uses the shared session so the __cf_bm cookie persists across URLs.
    Returns (status, redirect_to, note). status is an int HTTP code, or the
    string "ERROR" when the request itself failed (DNS, TLS, timeout, etc.).
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = session.request(
                method,
                url,
                impersonate=target,
                headers={"User-Agent": ua},
                allow_redirects=False,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - report any failure per-row
            last_exc = exc
            if attempt < retries:
                time.sleep(_RETRY_BACKOFF_SECONDS)
            continue
        # Retry block statuses now that the session may hold a fresh cookie.
        if resp.status_code in BLOCK_STATUSES and attempt < retries:
            time.sleep(_retry_after(resp))
            continue
        redirect_to = ""
        if 300 <= resp.status_code < 400:
            redirect_to = resp.headers.get("Location", "")
        # A trailing "?" — we can't prove the block is Cloudflare vs a genuine
        # 403/503, only that it survived our retry.
        note = "blocked?" if resp.status_code in BLOCK_STATUSES else ""
        return resp.status_code, redirect_to, note

    return "ERROR", "", f"{type(last_exc).__name__}: {last_exc}"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bulk-check URL statuses through a browser TLS fingerprint "
        "(defeats Cloudflare). Redirects are reported, not followed."
    )
    parser.add_argument("input", help="text file of URLs, one per line")
    parser.add_argument(
        "-o", "--output", default="url_status.csv",
        help="output CSV path (default: url_status.csv)",
    )
    parser.add_argument(
        "--impersonate", choices=sorted(CURRENT_TARGETS), default="chrome",
        help="browser family to impersonate (default: chrome)",
    )
    parser.add_argument(
        "--method", default="GET", choices=("GET", "HEAD"),
        help="HTTP method; GET is the default because Cloudflare often 403s "
        "bare HEAD requests. Use HEAD only for lighter checks on permissive "
        "hosts (default: GET)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.3,
        help="seconds to wait between requests, be polite (default: 0.3)",
    )
    parser.add_argument(
        # Matches the crawler's impersonated bound (run_spider's DOWNLOAD_TIMEOUT, #88).
        # This tool exists to REPRODUCE what the crawler saw, and it is what an operator
        # reaches for when triaging `unreachable.timeout` rows -- so a lower default here
        # makes a page the crawler would now fetch report ERROR, and the row gets closed as
        # "host down". That is the real-page-as-transport-failure mode #88 removed,
        # surviving in the tool used to confirm it was removed.
        "--timeout", type=float, default=60.0,
        help="per-request timeout in seconds (default: 60, matching the crawler)",
    )
    parser.add_argument(
        "--retries", type=int, default=1,
        help="retries on a Cloudflare block (403/429/503) once the session "
        "cookie is set; raise for flaky hosts (default: 1)",
    )
    args = parser.parse_args(argv)
    if args.retries < 0:
        parser.error("--retries must be >= 0")

    target = CURRENT_TARGETS[args.impersonate]
    ua = user_agent_for(target)
    urls = list(read_urls(args.input))
    if not urls:
        print("No URLs found in input file.", file=sys.stderr)
        return 1

    print(
        f"Checking {len(urls)} URLs as {target} ({args.method})...",
        file=sys.stderr,
    )

    session = requests.Session()  # persists __cf_bm across all URLs

    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["url", "status", "redirect_to", "note"])
        for i, url in enumerate(urls, 1):
            status, redirect_to, note = check(
                session, url, target, ua, args.method, args.timeout,
                args.retries,
            )
            writer.writerow([_csv_safe(url), status, _csv_safe(redirect_to), note])
            fh.flush()  # results survive a Ctrl-C mid-run
            print(
                f"[{i}/{len(urls)}] {status:>6}  {url}"
                + (f"  ->  {redirect_to}" if redirect_to else ""),
                file=sys.stderr,
            )
            if args.delay and i < len(urls):
                time.sleep(args.delay)

    print(f"\nDone. Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
