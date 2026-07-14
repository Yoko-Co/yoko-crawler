#!/usr/bin/env python3
"""Ad-hoc probe: can a real (headless) browser fetch a bot-protected site from THIS host?

Run this ON THE BOX whose IP you want to test (the DigitalOcean droplet) to learn whether a
server-side headless browser gets past a site that blocks the normal crawler -- BEFORE we
commit to building the full feature. It fetches the same URL several ways and prints a
per-method verdict (got real content vs blocked), so we can see WHICH layer the block is at:

  1. plain-http          urllib GET, no tricks           (baseline: bot-obvious)
  2. curl_cffi-chrome    the crawler's method today       (real Chrome TLS fingerprint,
                                                           NO JavaScript) -- should reproduce
                                                           the block we already see
  3. playwright-headless a real headless Chromium         (real browser + JavaScript)
  4. playwright-stealth  headless Chromium + light        (hides the headless tells)
                         anti-automation tweaks

Reading the result:
  - If #3/#4 get real content but #1/#2 are blocked -> the block is JavaScript/real-browser
    based, and a SERVER-SIDE headless browser will work (great: fully automatic, team-usable).
  - If #3/#4 are ALSO blocked (from this IP) -> the block is the datacenter IP (or headless
    detection), and a server-side browser won't help -- the crawl has to run from a trusted
    (your own) IP instead.

Setup on the droplet (once):
    python3 -m venv /tmp/probe-venv && . /tmp/probe-venv/bin/activate
    pip install playwright curl_cffi
    playwright install chromium
    playwright install-deps chromium      # Linux system libs; may need: sudo playwright install-deps chromium

Usage:
    python3 headless_probe.py https://urac.org/ [more urls...]
    # no args -> probes https://urac.org/ and https://www.urac.org/
"""

from __future__ import annotations

import ssl
import sys
import textwrap
import urllib.request

# This is a reachability probe, not secure transport: don't let a missing CA bundle (common
# on a bare box) turn a real HTTP answer into an SSL error and hide the result.
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE

# A current, realistic desktop-Chrome User-Agent (kept in step with a real Chrome release).
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Substrings that mark a Cloudflare/WAF challenge or block page rather than real content.
_BLOCK_MARKERS = (
    "just a moment", "attention required", "cf-chl", "cf_chl", "challenge-platform",
    "cloudflare", "enable javascript and cookies", "verify you are human",
    "checking your browser", "ki-cf-botcl", "access denied", "error 1020",
)


def _verdict(status: int | None, html: str, err: str | None = None) -> str:
    """One-line judgement for a fetched page."""
    if err:
        return f"ERROR      {err}"
    body = (html or "").lower()
    text_len = len(body)
    hit = next((m for m in _BLOCK_MARKERS if m in body), None)
    if status is not None and status >= 400:
        return f"BLOCKED    HTTP {status} (challenge/forbidden){' · ' + repr(hit) if hit else ''}"
    if hit:
        return f"BLOCKED    HTTP {status} but body looks like a challenge page ({hit!r})"
    if text_len < 1500:
        return f"SUSPICious HTTP {status}, but body is tiny ({text_len} chars) -- probably blocked/empty"
    return f"OK         HTTP {status}, {text_len:,} chars of real HTML -- looks like real content"


def probe_plain_http(url: str) -> tuple[int | None, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": CHROME_UA})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_UNVERIFIED_SSL) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:  # a 4xx/5xx is a real answer, not an error
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else "", None
    except Exception as exc:  # noqa: BLE001 -- a probe: report any failure, don't crash
        return None, "", f"{type(exc).__name__}: {exc}"


def probe_curl_cffi(url: str) -> tuple[int | None, str, str | None]:
    try:
        from curl_cffi import requests as cffi  # type: ignore
    except ImportError:
        return None, "", "curl_cffi not installed (pip install curl_cffi) -- skipped"
    try:
        resp = cffi.get(url, impersonate="chrome", timeout=30)
        return resp.status_code, resp.text, None
    except Exception as exc:  # noqa: BLE001
        return None, "", f"{type(exc).__name__}: {exc}"


def probe_playwright(url: str, *, stealth: bool) -> tuple[int | None, str, str | None]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return None, "", "playwright not installed (pip install playwright && playwright install chromium) -- skipped"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=CHROME_UA,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            if stealth:
                # The cheapest, best-known headless tells. Not a full stealth suite -- enough
                # to tell whether basic anti-automation flips the result.
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                )
            page = context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else None
            page.wait_for_timeout(5000)  # let any JS challenge run + redirect
            html = page.content()
            browser.close()
            return status, html, None
    except Exception as exc:  # noqa: BLE001
        return None, "", f"{type(exc).__name__}: {exc}"


def probe_url(url: str) -> None:
    print(f"\n=== {url} ===")
    methods = [
        ("plain-http", lambda: probe_plain_http(url)),
        ("curl_cffi-chrome", lambda: probe_curl_cffi(url)),
        ("playwright-headless", lambda: probe_playwright(url, stealth=False)),
        ("playwright-stealth", lambda: probe_playwright(url, stealth=True)),
    ]
    for name, fn in methods:
        status, html, err = fn()
        print(f"  {name:<22} {_verdict(status, html, err)}")


def main() -> None:
    urls = sys.argv[1:] or ["https://urac.org/", "https://www.urac.org/"]
    print(textwrap.dedent(
        """
        Headless-browser reachability probe -- run this ON the droplet (the IP under test).
        A method is 'OK' when it returns a real, substantial HTML page; 'BLOCKED' when it
        gets a 4xx/challenge or a tiny body. Compare the browser methods (playwright-*) to the
        no-JS methods (plain-http, curl_cffi) to see whether a real browser gets through.
        """
    ).strip())
    for url in urls:
        probe_url(url)
    print("\nDone. If the playwright-* rows are OK, a server-side headless browser works from")
    print("this IP. If they're BLOCKED too, the block is the IP -- run the crawl from a trusted one.")


if __name__ == "__main__":
    main()
