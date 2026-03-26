from __future__ import annotations

import scrapy
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin
from w3lib.url import canonicalize_url
from scrapy.http import TextResponse


class WebsiteSpider(scrapy.Spider):
    """
    Internal crawler that:
      - Treats base domain and www as internal; optional flag to allow all subdomains
      - Normalizes & de-duplicates URLs (drops fragments, strips junk params)
      - Records per-URL HTTP status, single-hop redirect target, and first referrer
      - Seeds from robots.txt → sitemap(s)
      - Can traverse paginated archives without recording each page:
          use -a reach_pagination=1
      - Alternatively, record pagination pages too:
          use -a keep_pagination=1
      - Allow other subdomains (besides www):
          use -a include_subdomains=1
    """

    name = "website_spider"
    allowed_domains = []
    start_urls = []

    # Ensure callbacks receive 3xx/4xx/5xx and don't auto-follow redirects
    custom_settings = {
        "REDIRECT_ENABLED": False,
        "HTTPERROR_ALLOW_ALL": True,
        # AUTOTHROTTLE and FEED_EXPORT_FIELDS are set by run_spider.py's
        # CrawlerProcess settings. Spider custom_settings have HIGHER
        # precedence, so they must NOT be set here or they silently override.
    }

    handle_httpstatus_all = True  # capture all status codes, including 3xx
    REDIRECT_STATUSES = {301, 302, 303, 307, 308}

    # File types to skip downloading/parsing (log only)
    ASSET_EXTENSIONS = {
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".rtf", ".txt", ".ics",
        ".odt", ".ods", ".odp",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".webp", ".ico",
        ".zip", ".rar", ".7z", ".tar", ".gz",
        ".mp3", ".wav", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv",
        ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    }

    # URL path segments that indicate login/auth pages (never yield useful content)
    LOGIN_PATH_SEGMENTS = {
        "wp-login.php", "wp-admin",
        "login", "signin", "sign-in", "sign_in",
        "logout", "signout", "sign-out", "sign_out",
        "auth", "oauth", "oauth2", "sso", "cas", "saml", "adfs",
    }

    # Query parameters commonly used for tracking, sessions, or cache busting
    UNWANTED_PARAMS = {
        # Tracking / analytics
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_reader", "utm_name", "utm_social", "utm_place",
        "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "icid",
        "ga_source", "ga_medium", "ga_campaign", "ga_term", "ga_content",
        "hsa_acc", "hsa_cam", "hsa_grp", "hsa_ad", "hsa_src", "hsa_tgt",
        "hsa_kw", "hsa_net", "hsa_mt",
        # Session/cache junk
        "sessionid", "sid", "phpsessid", "jsessionid", "_ga", "_gl", "_ke",
        "_hsenc", "_hsmi", "sc_cid", "ver",
        # Rendering / redirects
        "sfvrsn", "returnurl", "redirect", "ref", "ref_", "refid",
        "referer", "r", "rid", "v", "view", "mode", "preview",
        # Social/email shares
        "share", "socialshare", "emcid", "emc", "elqtrackid",
        "elqtrack", "mkt_tok",
        # Cache busting / random
        "nocache", "cachebust", "cb", "rnd", "random", "_ts",
        "timestamp", "t",
        # Pagination/sorting (toggleable)
        "page", "p", "offset", "start", "sort", "order", "dir",
    }

    # Separable so we can treat pagination differently for scheduling vs emitting
    PAGINATION_PARAMS = {"page", "p", "offset", "start", "sort", "order", "dir"}

    def __init__(self, *args, **kwargs):
        """
        Spider args:
          - domain=example.org  Required base domain (and start URL).
          - reach_pagination=1  Traverse paginated pages but DO NOT record each one.
          - keep_pagination=1   Record paginated pages too (treat as unique).
          - include_subdomains=1  Treat any subdomain of the base domain as internal.
        """
        super().__init__(*args, **kwargs)
        self.seen = set()              # scheduled (normalized in schedule-mode)
        self.emitted = set()           # already written (normalized in emit-mode)
        self.first_referrer = {}       # schedule-norm URL -> emit-norm first referrer

        keep_pagination = str(kwargs.get("keep_pagination", "")).lower() in {"1", "true", "yes"}
        self.reach_pagination = str(kwargs.get("reach_pagination", "")).lower() in {"1", "true", "yes"}
        self.include_subdomains = str(kwargs.get("include_subdomains", "")).lower() in {"1", "true", "yes"}

        domain_arg = kwargs.get("domain")
        if not domain_arg:
            raise ValueError("domain is required. Use -a domain=example.org")
        domain = str(domain_arg).strip().lower().rstrip(".")
        if not domain:
            raise ValueError("domain is required. Use -a domain=example.org")
        self.base_domain = domain
        if self.include_subdomains:
            self.allowed_domains = [domain]
        else:
            self.allowed_domains = [domain, f"www.{domain}"]
        self.start_urls = [f"https://{domain}/"]

        # Build exclude sets for scheduling vs emitting
        if self.reach_pagination:
            # Visit distinct pagination pages, but normalize them away when emitting
            self.exclude_params_schedule = set(self.UNWANTED_PARAMS) - self.PAGINATION_PARAMS
            self.exclude_params_emit = set(self.UNWANTED_PARAMS)
        else:
            # Original behavior (optionally keep/drop pagination everywhere)
            base = set(self.UNWANTED_PARAMS)
            if keep_pagination:
                base -= self.PAGINATION_PARAMS
            self.exclude_params_schedule = base
            self.exclude_params_emit = base

    # ---------- URL helpers ----------

    def is_internal(self, url: str) -> bool:
        """Accept bare domain or www; optionally allow any subdomain of base domain."""
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if self.include_subdomains:
            return host == self.base_domain or host.endswith(f".{self.base_domain}")
        return host in {self.base_domain, f"www.{self.base_domain}"}

    def strip_unwanted_queries(self, url: str, *, exclude_params) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        for key in list(query.keys()):
            if key.lower() in exclude_params:
                query.pop(key, None)
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def normalize_url(self, url: str, *, exclude_params) -> str:
        cleaned = self.strip_unwanted_queries(url, exclude_params=exclude_params)
        return canonicalize_url(cleaned, keep_fragments=False)

    def is_asset_url(self, url: str) -> bool:
        path = (urlparse(url).path or "").lower()
        return any(path.endswith(ext) for ext in self.ASSET_EXTENSIONS)

    def is_login_url(self, url: str) -> bool:
        """Detect login/auth URLs by checking path segments against known patterns."""
        path = (urlparse(url).path or "").lower()
        segments = path.split("/")
        return any(seg in self.LOGIN_PATH_SEGMENTS for seg in segments)

    # ---------- Entry points ----------

    def start_requests(self):
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse)
        yield scrapy.Request(urljoin(self.start_urls[0], "/robots.txt"), callback=self.parse_robots)

    # ---------- Robots & sitemaps ----------

    def parse_robots(self, response):
        # Record robots fetch
        yield from self._emit_row(response)

        # One-hop redirect follow
        if response.status in self.REDIRECT_STATUSES:
            target = response.headers.get("Location")
            if target:
                yield scrapy.Request(response.urljoin(target.decode("latin-1")), callback=self.parse_robots)
            return

        # Discover sitemaps
        for line in response.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if sm_url:
                    yield scrapy.Request(sm_url, callback=self.parse_sitemap, dont_filter=True)

    def parse_sitemap(self, response):
        # Record sitemap fetch
        yield from self._emit_row(response)

        # One-hop redirect follow
        if response.status in self.REDIRECT_STATUSES:
            target = response.headers.get("Location")
            if target:
                yield scrapy.Request(response.urljoin(target.decode("latin-1")), callback=self.parse_sitemap)
            return

        # Skip non-text sitemaps like .gz
        if not isinstance(response, TextResponse):
            self.logger.info("Skipping non-text sitemap: %s", response.url)
            return

        # Pull <loc> values from XML (supports sitemap + index)
        for loc in response.xpath("//*[local-name()='loc']/text()").getall():
            if self.is_internal(loc):
                yield from self._schedule(loc, referrer_emit=self.normalize_url(response.url, exclude_params=self.exclude_params_emit))

        # Follow nested sitemap indexes if present
        for sm in response.xpath("//*[local-name()='sitemap']/*[local-name()='loc']/text()").getall():
            if self.is_internal(sm):
                yield scrapy.Request(sm, callback=self.parse_sitemap, dont_filter=True)

    # ---------- Main parse ----------

    def parse(self, response):
        # Emit the fetched page once (using emit-mode normalization)
        yield from self._emit_row(response)

        # If redirect, schedule the single hop and stop parsing this page
        if response.status in self.REDIRECT_STATUSES:
            loc = response.headers.get("Location")
            if loc:
                yield from self._schedule(
                    response.urljoin(loc.decode("latin-1")),
                    referrer_emit=self.normalize_url(response.url, exclude_params=self.exclude_params_emit),
                )
            return

        # Only parse links from text-like responses
        if not isinstance(response, TextResponse):
            return

        # Extra content-type guard for odd servers
        ctype = (response.headers.get("Content-Type") or b"").decode("latin-1").lower()
        if "html" not in ctype and "xml" not in ctype:
            return

        # Collect a richer set of link sources
        selectors = [
            "a[href]", "area[href]",
            "link[rel='next'][href]", "link[rel='prev'][href]",
            "link[rel='canonical'][href]", "link[rel='alternate'][href]",
        ]
        current_emit = self.normalize_url(response.url, exclude_params=self.exclude_params_emit)

        for sel in response.css(", ".join(selectors)):
            href = sel.attrib.get("href")
            if not href:
                continue
            full_url = response.urljoin(href)
            if self.is_internal(full_url):
                yield from self._schedule(full_url, referrer_emit=current_emit)

    # ---------- Helpers ----------

    def _emit_row(self, response):
        """
        Write one JSONL row for the fetched URL with:
          url, status, last_modified, redirected_to, referrer (first seen)
        Emission uses emit-mode normalization (pagination stripped when reach_pagination=1).
        """
        current_emit = self.normalize_url(response.url, exclude_params=self.exclude_params_emit)
        if current_emit in self.emitted:
            return

        status = int(response.status)
        last_modified = response.headers.get("Last-Modified", b"").decode("latin-1").strip()

        # Redirect target (single-hop), normalized in emit-mode
        redirected_to = ""
        if status in self.REDIRECT_STATUSES:
            loc = response.headers.get("Location")
            if loc:
                redirected_to = self.normalize_url(
                    response.urljoin(loc.decode("latin-1")),
                    exclude_params=self.exclude_params_emit
                )

        # First referrer, if we have it (prefer what we captured at schedule time)
        current_schedule = self.normalize_url(response.url, exclude_params=self.exclude_params_schedule)
        referrer = self.first_referrer.get(current_schedule, "")

        # Fallback to actual Referer header if not captured earlier
        if not referrer:
            hdr_ref = response.request.headers.get(b"Referer")
            if hdr_ref:
                try:
                    referrer = self.normalize_url(
                        response.urljoin(hdr_ref.decode("latin-1")),
                        exclude_params=self.exclude_params_emit
                    )
                except Exception:
                    referrer = ""

        self.emitted.add(current_emit)
        yield {
            "url": current_emit,
            "status": status,
            "last_modified": last_modified,
            "redirected_to": redirected_to,
            "referrer": referrer,
        }

    def _schedule(self, url, *, referrer_emit: str | None = None):
        """
        Normalize with schedule-mode (pagination retained when reach_pagination=1),
        de-dup, and enqueue the next request. Record the first referrer seen.
        """
        if not self.is_internal(url):
            return
        normalized = self.normalize_url(url, exclude_params=self.exclude_params_schedule)
        if normalized in self.seen:
            return

        if self.is_login_url(normalized):
            self.seen.add(normalized)
            self.crawler.stats.inc_value("login_urls_skipped")
            self.logger.debug("Skipping login/auth URL: %s", normalized)
            return

        if self.is_asset_url(normalized):
            self.seen.add(normalized)
            if referrer_emit:
                self.first_referrer.setdefault(normalized, referrer_emit)
            self.logger.info("Fetching headers for asset URL: %s", normalized)
            yield scrapy.Request(
                normalized,
                callback=self.parse_asset,
                method="HEAD",
                dont_filter=True,
            )
            return

        # Store first referrer for this scheduled target (schedule-norm key, emit-norm value)
        if referrer_emit:
            self.first_referrer.setdefault(normalized, referrer_emit)

        self.seen.add(normalized)
        yield scrapy.Request(normalized, callback=self.parse, dont_filter=True)

    def parse_asset(self, response):
        """
        Record asset metadata from headers only (HEAD request).
        """
        yield from self._emit_row(response)

        if response.status in self.REDIRECT_STATUSES:
            loc = response.headers.get("Location")
            if loc:
                yield from self._schedule(
                    response.urljoin(loc.decode("latin-1")),
                    referrer_emit=self.normalize_url(response.url, exclude_params=self.exclude_params_emit),
                )
