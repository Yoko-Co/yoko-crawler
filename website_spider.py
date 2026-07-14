from __future__ import annotations

import json
import re

import scrapy
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin
from w3lib.url import canonicalize_url
from scrapy.http import TextResponse

from content_extractor import (
    content_hash,
    component_signals,
    count_structure,
    embed_signals,
    empty_enrichment,
    extract_content,
)
from embed_allowlist import load_benign_hosts

# Zero/empty enrichment defaults come from content_extractor.empty_enrichment()
# (the single source of truth for field names). content_text is handled
# separately: present only when --emit-content is set.


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

    # HTTP statuses a bot-wall (Cloudflare/WAF) serves a challenge/block page on. The
    # challenge page is real HTML with a body, so without a guard the crawler would
    # extract its markup as "content" and follow its links (e.g. Kinsta's
    # `?ki-cf-botcl=1` challenge URL) — polluting the crawl with the wall's own pages.
    # A challenge row is still EMITTED (its 403/429 status is the signal the corpus reads
    # to detect a bot-block and retry with impersonation), just not mined for content/links.
    WAF_CHALLENGE_STATUSES = {403, 429, 503}

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

    # WordPress infrastructure endpoints (machine-only, no redirect value)
    INFRA_PATH_SEGMENTS = {
        "wp-json", "xmlrpc.php", "wp-cron.php", "trackback",
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
        # Bot-wall challenge tokens: Kinsta+Cloudflare (`?ki-cf-botcl=1`) and Cloudflare's
        # own challenge query tokens. These are the WALL's URLs, never real content pages;
        # stripping them keeps a stray challenge link from being recorded as a distinct page
        # (the challenge pages themselves are also not followed -- see WAF_CHALLENGE_STATUSES).
        "ki-cf-botcl", "__cf_chl_rt_tk", "__cf_chl_tk", "__cf_chl_jschl_tk__",
        "__cf_chl_f_tk", "cf_chl_rt_tk",
        # WordPress / CMS non-content: on-site search (?s= renders the SAME page and was
        # doubling whole crawls -- e.g. every GVF page appeared as /x/ AND /x/?s=), plus
        # comment-reply/moderation links. Search-results variants are not content pages, so
        # collapsing any ?s= value onto the base URL is correct.
        "s", "search", "replytocom", "unapproved", "moderation-hash",
        # Pagination/sorting (toggleable)
        "page", "p", "offset", "start", "sort", "order", "dir",
    }

    # Separable so we can treat pagination differently for scheduling vs emitting
    PAGINATION_PARAMS = {"page", "p", "offset", "start", "sort", "order", "dir"}

    # <link rel=canonical> href, matched case-insensitively and as a whitespace-separated
    # token (so `rel="canonical alternate"` and `rel="CANONICAL"` both match). issue #10.
    _CANONICAL_XPATH = (
        "//link[contains(concat(' ', "
        "translate(@rel, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "' '), ' canonical ')]/@href"
    )

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

        # Injected cookies: reuse a browser-solved Cloudflare clearance cookie. A raw
        # Cookie-header string ("cf_clearance=...; __cf_bm=...") is parsed to a dict and
        # attached to the seed requests; Scrapy's cookie jar (COOKIES_ENABLED default True)
        # then re-sends them on every followed request to the same domain, so a
        # cf_clearance cookie carries through the whole crawl. Pair with a matching
        # --user-agent -- cf_clearance is bound to the UA (and usually the IP) that solved
        # the challenge, so a mismatched UA (or a different egress IP) is rejected.
        self.injected_cookies = self._parse_cookie_string(kwargs.get("cookies"))

        # Content enrichment options.
        self.emit_content = str(kwargs.get("emit_content", "")).lower() in {"1", "true", "yes"}
        # Needed so iframe_hosts (a list) can be JSON-encoded for CSV output,
        # where a real array can't round-trip. Defaults to jsonlines.
        self.output_format = str(kwargs.get("output_format", "jsonlines")).lower()
        # Resolve the benign-embed allowlist once per crawl.
        self.benign_hosts = load_benign_hosts()

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

    @staticmethod
    def _parse_cookie_string(raw) -> dict:
        """Parse a raw Cookie-header string ("a=1; b=2") into a {name: value} dict.
        Tolerant: splits pairs on ';' and each pair on the FIRST '=' (a cookie value can
        itself contain '=', e.g. base64), trims whitespace, and skips empty/malformed
        pairs. Returns {} for None/empty input."""
        cookies = {}
        for part in str(raw or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                cookies[name] = value.strip()
        return cookies

    # ---------- URL helpers ----------

    def is_internal(self, url: str) -> bool:
        """Accept bare domain or www; optionally allow any subdomain of base domain."""
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if self.include_subdomains:
            return host == self.base_domain or host.endswith(f".{self.base_domain}")
        return host in {self.base_domain, f"www.{self.base_domain}"}

    # Non-navigational URI schemes: never page URLs. A well-formed one (e.g. `mailto:`)
    # already fails is_internal (no host), but a MALFORMED one -- `mail to:info@x`, a
    # `mailto:` link with a stray space -- would otherwise be urljoin'd into a crawlable
    # path (`.../mail%20to:info@x`), which is exactly the junk the GVF crawl followed.
    _NONNAV_SCHEMES = (
        "mailto:", "tel:", "callto:", "sms:", "whatsapp:", "javascript:",
        "data:", "blob:", "file:", "ftp:", "ftps:",
    )

    def is_navigational_href(self, href: str) -> bool:
        """Whether a raw <a href> is worth following as a page URL. Rejects empty and
        fragment-only hrefs and non-navigational schemes -- INCLUDING malformed ones a
        literal space or a `%20` would otherwise smuggle past urljoin as a relative path.
        Whitespace/`%20` is collapsed only to detect the SCHEME; the real urljoin still
        uses the original href."""
        if not href:
            return False
        # Collapse literal AND percent-encoded whitespace (and a leading BOM) so a scheme
        # split by any of them still resolves -- `mail to:`, `mail%20to:`, `mail%09to:`.
        collapsed = re.sub(r"(?:\s|%20|%09|%0a|%0d)+", "", href, flags=re.IGNORECASE)
        collapsed = collapsed.lstrip("\ufeff\u200b\x00").lower()  # BOM / zero-width / NUL
        if not collapsed or collapsed.startswith("#"):
            return False
        return not collapsed.startswith(self._NONNAV_SCHEMES)

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

    def is_infra_url(self, url: str) -> bool:
        """Detect WordPress infrastructure endpoints (REST API, XML-RPC, cron, trackback)."""
        path = (urlparse(url).path or "").lower()
        segments = path.split("/")
        return any(seg in self.INFRA_PATH_SEGMENTS for seg in segments)

    # ---------- Entry points ----------

    def start_requests(self):
        # Seed the cookie jar with any injected cookies (e.g. a browser-solved
        # cf_clearance): setting them on the seed requests lets Scrapy's CookiesMiddleware
        # re-attach them to every followed request to the same domain automatically.
        cookies = self.injected_cookies or None
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse, cookies=cookies)
        yield scrapy.Request(
            urljoin(self.start_urls[0], "/robots.txt"),
            callback=self.parse_robots,
            cookies=cookies,
        )

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

        # A bot-wall challenge/block page (Cloudflare/WAF): the row is emitted (its 403/429
        # is the signal the corpus reads), but we do NOT follow its links -- they are the
        # wall's own challenge URLs (e.g. `?ki-cf-botcl=1`), not the site's navigation.
        if self._is_waf_challenge(response):
            self.crawler.stats.inc_value("waf_challenge_count")
            return

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
            if not self.is_navigational_href(href):
                # empty/fragment-only, or a non-navigational scheme (mailto/tel/js/...),
                # including a malformed one -- don't urljoin it into a crawlable path.
                if href:
                    self.crawler.stats.inc_value("nonnav_hrefs_skipped")
                continue
            full_url = response.urljoin(href)
            if self.is_internal(full_url):
                yield from self._schedule(full_url, referrer_emit=current_emit)

    # ---------- Helpers ----------

    def _is_waf_challenge(self, response) -> bool:
        """True when a response is a Cloudflare/WAF bot-wall challenge or block page,
        not a real page. Keyed on a challenge status (403/429/503) PLUS a Cloudflare
        fingerprint so an ordinary application 403/503 (a protected page, a brief
        maintenance blip) isn't mistaken for a wall:
          - `cf-mitigated` header — Cloudflare stamps it on challenge/block responses; or
          - a `cf-ray` header AND `server: cloudflare` — a Cloudflare-fronted response at a
            challenge status (Kinsta fronts with Cloudflare and adds the `ki-cf-botcl` param).
        Conservative on purpose: a false positive only costs us one page's content/links
        (already unreadable at 403), while a false negative re-introduces the wall-page
        pollution this guards against."""
        if response.status not in self.WAF_CHALLENGE_STATUSES:
            return False
        headers = response.headers
        if headers.get("cf-mitigated"):
            return True
        server = (headers.get("Server") or b"").decode("latin-1").lower()
        return bool(headers.get("cf-ray")) and "cloudflare" in server

    def _enrichment(self, response):
        """Compute the additive content/structural fields for a row.

        HTML pages (a non-redirect TextResponse with an html content-type and a
        body) get real counts, page-wide embed signals, and a main-content-scoped
        content hash. Every other row -- assets fetched HEAD-only, non-HTML
        responses, redirects -- gets zero/empty defaults so all rows share one
        shape.

        The whole computation is guarded: if extraction/counting raises on a
        pathological page, the row still emits with empty enrichment defaults so
        the original five fields are never lost (the backward-compat guarantee).
        """
        ctype = (response.headers.get("Content-Type") or b"").decode("latin-1").lower()
        is_html_page = (
            isinstance(response, TextResponse)
            and "html" in ctype
            and response.status not in self.REDIRECT_STATUSES
            and bool(response.body)
            # A WAF challenge page is HTML with a body, but its markup is the wall's, not
            # the site's — don't extract it as content (a bogus word_count/content_hash
            # would make a bot-blocked page look like a real "simple" page downstream).
            and not self._is_waf_challenge(response)
        )

        content_text = ""
        if is_html_page:
            try:
                result = extract_content(response.body)
                counts = count_structure(
                    result.subtree,
                    response.url,
                    is_internal=self.is_internal,
                    asset_extensions=self.ASSET_EXTENSIONS,
                )
                # Embeds are page-wide: surprising iframes live in headers,
                # footers, and sidebars, not just the main content region.
                signals = embed_signals(result.body_subtree, self.benign_hosts)
                # Interactive JS components are page-wide too (issue #12).
                components = component_signals(result.body_subtree)
                fields = empty_enrichment()
                fields.update(counts)
                fields["content_hash"] = content_hash(result.normalized_text)
                fields["main_content_extracted"] = result.main_content_extracted
                fields["embed_count_nonbenign"] = signals["embed_count_nonbenign"]
                fields["component_count"] = components["component_count"]
                fields["iframe_hosts"] = signals["iframe_hosts"]
                content_text = result.normalized_text
            except Exception:
                # Never let one bad page drop the row (and its original five
                # fields). Emit empty enrichment and log for diagnosis.
                self.logger.exception("Enrichment failed for %s", response.url)
                fields = empty_enrichment()
                content_text = ""
            # Canonical (issue #10) is a <head> concern, independent of the body-scoped
            # counts -- extract it separately (reusing parsel's already-parsed tree) and
            # best-effort, so a bad canonical can never drop the row's real counts. XPath
            # (not [rel='canonical']) so a multi-token `rel="canonical alternate"` or an
            # uppercase `rel` still matches: lowercase then whitespace-token membership.
            try:
                canon_href = response.xpath(self._CANONICAL_XPATH).get()
                if canon_href and canon_href.strip():
                    fields["canonical"] = self.normalize_url(
                        response.urljoin(canon_href.strip()),
                        exclude_params=self.exclude_params_emit,
                    )
            except Exception:
                self.logger.debug("canonical extraction failed for %s", response.url)
        else:
            fields = empty_enrichment()

        # CSV can't hold a real array; JSON-encode iframe_hosts so it round-trips.
        # jsonlines keeps the native array (what yoko-corpus consumes).
        if self.output_format == "csv":
            fields["iframe_hosts"] = json.dumps(fields["iframe_hosts"])

        # content_text is the one conditional field: present only with
        # --emit-content (absent means "not requested", not "empty").
        if self.emit_content:
            fields["content_text"] = content_text

        return fields

    def _emit_row(self, response):
        """
        Write one JSONL row for the fetched URL with:
          url, status, last_modified, redirected_to, referrer (first seen)
        plus the additive content/structural enrichment fields.
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
        row = {
            "url": current_emit,
            "status": status,
            "last_modified": last_modified,
            "redirected_to": redirected_to,
            "referrer": referrer,
        }
        row.update(self._enrichment(response))
        yield row

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

        if self.is_infra_url(normalized):
            self.seen.add(normalized)
            self.crawler.stats.inc_value("infra_urls_skipped")
            self.logger.debug("Skipping infrastructure URL: %s", normalized)
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
