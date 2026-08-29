"""Downloader middleware that routes every request through a forward proxy (issue #22).

Sets `request.meta["proxy"]` on each request, which BOTH download handlers honor: Scrapy's
default handler and the curl_cffi/scrapy-impersonate handler (its `parser.py` reads
`request.meta.get("proxy")` and configures curl for HTTP/HTTPS/SOCKS, with auth). We set it
in one place so it covers every request the spider makes -- seeds, scheduled pages, redirect
hops, sitemaps, asset HEADs -- with no per-Request wiring to miss.

The proxy is TRANSPORT only. The SSRF guard runs first and resolves the request's TARGET host,
blocking private/reserved ranges regardless of the proxy -- so a trusted proxy can never be
turned into a relay into a private network from the crawler's side. (The box itself should also
firewall RFC1918 destinations as belt-and-suspenders; see the #22 plan.)

Enabled only when `YOKO_CRAWL_PROXY` is set (run_spider sets it from `--proxy`); otherwise
`NotConfigured`, so a normal crawl is byte-identical to before.
"""

from scrapy.exceptions import NotConfigured


class ProxyMiddleware:
    def __init__(self, proxy: str):
        self.proxy = proxy

    @classmethod
    def from_crawler(cls, crawler):
        proxy = crawler.settings.get("YOKO_CRAWL_PROXY")
        if not proxy:
            raise NotConfigured  # no proxy configured -> middleware not installed
        return cls(proxy)

    def process_request(self, request, spider):
        # `setdefault` so an explicit per-request proxy (none today) would still win.
        request.meta.setdefault("proxy", self.proxy)
        return None
