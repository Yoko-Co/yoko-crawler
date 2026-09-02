"""Downloader middleware that drops requests resolving to blocked address ranges.

Defense-in-depth against DNS rebinding/SSRF. The API validates the domain at
submit time (domain_validator), but the crawl subprocess re-resolves DNS at
fetch time and connects without re-checking. This middleware re-checks each
distinct host against the blocked ranges immediately before download, covering
both the default Scrapy handler and the curl_cffi (impersonate) handler, since
downloader middlewares run regardless of which download handler is active.

Residual: this re-validates resolution but does not pin the IP, so a determined
active rebind within the resolve->connect window is not fully closed. Hosts are
cached after the first check, so a single crawl resolves each host once.
"""

from urllib.parse import urlparse

from scrapy.exceptions import IgnoreRequest

from domain_validator import host_resolves_to_blocked


class SsrfGuardMiddleware:
    def __init__(self, stats=None):
        # host -> bool(blocked); avoids re-resolving the same host every request.
        self._checked = {}
        # Scrapy stats collector; bumped on each block so ProgressWriter can tell
        # a crawl that was blocked into emptiness from a genuinely empty site.
        self._stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        return cls(stats=crawler.stats)

    # Schemes this crawler is ever allowed to fetch. Mirrors
    # WebsiteSpider._FETCHABLE_SCHEMES deliberately -- see the note in process_request.
    _FETCHABLE_SCHEMES = frozenset({"http", "https"})

    def process_request(self, request, spider):
        parsed = urlparse(request.url)
        # Refuse a non-http(s) scheme outright (issue #89). The host check below cannot do
        # this job: it asks whether a HOSTNAME resolves to a blocked address, and
        # `file://<a-real-public-domain>/etc/passwd` has a hostname that resolves perfectly
        # well -- while Scrapy's FileDownloadHandler ignores that host and reads the local
        # path. So "the host is fine" and "this request is safe to hand to a download
        # handler" are different questions, and only the first was being asked.
        #
        # `is_internal` is the primary gate and rejects these before they are ever built;
        # this is the second layer, because that gate lives on the spider and a future
        # request built anywhere else would not pass through it. The duplicated constant is
        # the point -- importing the spider's would couple a security guard to the module
        # it exists to be independent of.
        if parsed.scheme.lower() not in self._FETCHABLE_SCHEMES:
            if self._stats is not None:
                self._stats.inc_value("ssrf_guard/blocked_scheme")
            raise IgnoreRequest(
                f"SSRF guard: refusing non-http(s) scheme {parsed.scheme!r} "
                f"({request.url[:120]})"
            )
        host = parsed.hostname
        if not host:
            return None
        blocked = self._checked.get(host)
        if blocked is None:
            blocked = host_resolves_to_blocked(host)
            self._checked[host] = blocked
        if blocked:
            if self._stats is not None:
                self._stats.inc_value("ssrf_guard/blocked")
            raise IgnoreRequest(
                f"SSRF guard: {host} resolves to a blocked/reserved address"
            )
        return None
