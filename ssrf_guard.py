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
    def __init__(self):
        # host -> bool(blocked); avoids re-resolving the same host every request.
        self._checked = {}

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_request(self, request, spider):
        host = urlparse(request.url).hostname
        if not host:
            return None
        blocked = self._checked.get(host)
        if blocked is None:
            blocked = host_resolves_to_blocked(host)
            self._checked[host] = blocked
        if blocked:
            raise IgnoreRequest(
                f"SSRF guard: {host} resolves to a blocked/reserved address"
            )
        return None
