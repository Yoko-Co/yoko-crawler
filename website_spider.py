from __future__ import annotations

import json
import os
import re

import scrapy
from protego import Protego
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse, urljoin
from w3lib.url import canonicalize_url
from scrapy.http import TextResponse

from content_extractor import (
    content_hash,
    component_signals,
    count_structure,
    embed_signals,
    empty_enrichment,
    extract_content,
    script_signals,
    slider_signals,
    structure_hash,
)
from embed_allowlist import load_benign_hosts
from script_allowlist import load_benign_script_hosts

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
      - Contains faceted search: collapses facet slot-order permutations and skips
        filter selections deeper than MAX_FACET_DEPTH (issue #49):
          use -a max_facet_depth=N
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

    # The robots.txt user-agent group we obey. We present a browser fingerprint, not a named
    # bot, so we fall under the catch-all `*` group -- and a site that names us explicitly
    # (`User-agent: yoko-crawler`) is honored too, since protego picks the best-matching group
    # and this token matches nothing else, falling back to `*`.
    ROBOTS_USER_AGENT = "yoko-crawler"
    # Cap on an honored robots.txt Crawl-delay (seconds). We never crawl faster than a site
    # asks, but a pathological delay (e.g. 3600s) would blow the crawl budget, so we honor up
    # to this and log the clamp. Env-tunable; the site's own smaller delay is honored in full.
    DEFAULT_MAX_ROBOTS_CRAWL_DELAY = 10.0

    # CMS/CDN infrastructure endpoints (machine-only, never site content, no redirect
    # value). `cdn-cgi` is Cloudflare-reserved: nothing under it is the origin's content
    # -- it fronts AI Labyrinth crawler-trap pages (fake AI-written articles behind
    # invisible nofollow links), email-obfuscation, image-resizing, and trace endpoints.
    # Following the Labyrinth is what fingerprints a client as a bot, so we never enter it.
    INFRA_PATH_SEGMENTS = {
        "wp-json", "xmlrpc.php", "wp-cron.php", "trackback",
        "cdn-cgi",
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
        # Pagination/sorting (toggleable). The sort family includes Drupal Views' exposed-sort
        # param names (`sort_by`/`sort_order`, and Better Exposed Filters' `sort_bef_combine`),
        # not just the short generic ones -- naeyc.org sorts listings with `?sort_by=...&sort_order=DESC`.
        "page", "p", "offset", "start",
        "sort", "order", "dir", "sort_by", "sort_order", "sort_bef_combine",
    }

    # Separable so we can treat pagination differently for scheduling vs emitting.
    # Two distinct classes hide inside "pagination", and only ONE reveals new content:
    #   - SEQUENCE_PARAMS advance through a listing (`?page=2`, `?offset=20`) -- each value
    #     surfaces a DIFFERENT set of items, so following them is a real discovery path
    #     (issue #58: naeyc's 19-page Drupal blog had ~2/3 of its posts behind `?page=`).
    #   - REORDER_PARAMS only re-sort the SAME items (`?sort=title&order=desc`, or Drupal
    #     Views' `?sort_by=field_date&sort_order=DESC`); every value is a view of one result
    #     set. Following them multiplies a listing by every sort permutation (page x sort x
    #     order) for zero new content, so they stay stripped in EVERY mode -- they live only
    #     in UNWANTED_PARAMS below, never in the keep-set.
    SEQUENCE_PARAMS = {"page", "p", "offset", "start"}
    REORDER_PARAMS = {"sort", "order", "dir", "sort_by", "sort_order", "sort_bef_combine"}
    # The historical union. No production code reads it anymore (the exclude-set
    # construction subtracts SEQUENCE_PARAMS directly); kept as the back-compat name and
    # for the partition-invariant guard test, so a future reader knows it isn't dead.
    PAGINATION_PARAMS = SEQUENCE_PARAMS | REORDER_PARAMS

    # Faceted search (issue #49). A multi-select facet UI emits one query param per
    # selected filter, which fans out combinatorially: every SUBSET is a URL, and every
    # ORDERING of a subset is another URL. On naeyc.org that turned one Drupal Search API
    # page into 1,491 crawled URLs -- 77.6% of the crawl, leaving the real 430-page site
    # under-crawled -- and made one search page contribute 25 of the 30 "pages with forms".
    #
    # An indexed array param: `f[0]`, `tid[2]`, `field_topics[1]`. The trailing [N] is a
    # slot number, so the base name identifies the family. Anchored both ends so a param
    # merely CONTAINING brackets isn't misread as a facet.
    _FACET_INDEX_RE = re.compile(r"^(.+?)\[\d+\]$")
    # Bare facet params used without an index by common search stacks (Solr/Search API,
    # Algolia, WooCommerce-style filters).
    FACET_PARAM_NAMES = {"fq", "facet", "facets", "filter", "filters",
                         "refine", "refinement", "refinements"}
    # Filters deep enough to be a duplicate VIEW of a result set rather than a distinct
    # page. Keeps the unfiltered page plus shallow combinations (on naeyc.org: 5 URLs
    # instead of 1,491) -- a redesign builds the search template once, not once per filter
    # combination. Override with -a max_facet_depth=N.
    MAX_FACET_DEPTH = 2

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
        # Dedup state. Rebound on first use into `self.state` so it survives between
        # resumable sessions (issue #52; see `_bind_dedup_state`) -- these plain values are
        # what a crawl with no JOBDIR uses, and the fallback if the extension never fires.
        self.seen = set()              # scheduled (normalized in schedule-mode)
        self.emitted = set()           # already written (normalized in emit-mode)
        self.first_referrer = {}       # schedule-norm URL -> emit-norm first referrer
        self._state_bound = False      # guards the one-time bind

        # Faceted-search depth cap (issue #49). Non-numeric/negative input falls back to
        # the class default rather than failing the crawl or disabling the cap.
        try:
            self.max_facet_depth = int(kwargs.get("max_facet_depth", self.MAX_FACET_DEPTH))
        except (TypeError, ValueError):
            self.max_facet_depth = self.MAX_FACET_DEPTH
        if self.max_facet_depth < 0:
            self.max_facet_depth = self.MAX_FACET_DEPTH

        keep_pagination = str(kwargs.get("keep_pagination", "")).lower() in {"1", "true", "yes"}
        self.reach_pagination = str(kwargs.get("reach_pagination", "")).lower() in {"1", "true", "yes"}
        self.include_subdomains = str(kwargs.get("include_subdomains", "")).lower() in {"1", "true", "yes"}

        # Content enrichment options.
        self.emit_content = str(kwargs.get("emit_content", "")).lower() in {"1", "true", "yes"}
        # Needed so iframe_hosts (a list) can be JSON-encoded for CSV output,
        # where a real array can't round-trip. Defaults to jsonlines.
        self.output_format = str(kwargs.get("output_format", "jsonlines")).lower()
        # Resolve the benign-embed and benign-script allowlists once per crawl.
        self.benign_hosts = load_benign_hosts()
        self.benign_script_hosts = load_benign_script_hosts()

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
        # The site's own hosts -- a same-site <script src> is the site's own code, not a
        # third-party integration, so script_signals skips it (issue #28).
        self.self_hosts = frozenset(self.allowed_domains)

        # robots.txt obedience (issues #57/#59): a Protego matcher parsed from the site's
        # robots.txt when the seed fetch returns it. None until then (and for a site with
        # no robots.txt) -> allow-all, unchanged behavior. Disallow rules gate scheduling;
        # a Crawl-delay paces the host (see parse_robots / _apply_crawl_delay).
        self._robots = None
        # Start URLs are emitted from parse_robots / robots_failed, never from the seed
        # itself (issue #76), so robots rules are always known before any page URL is
        # scheduled. A robots.txt redirect chain re-enters parse_robots, so this guard
        # keeps the start URLs emitted exactly once no matter how many times we land here.
        self._start_urls_emitted = False
        try:
            self.max_robots_crawl_delay = float(
                os.environ.get("YOKO_CRAWL_MAX_ROBOTS_DELAY", self.DEFAULT_MAX_ROBOTS_CRAWL_DELAY)
            )
        except (TypeError, ValueError):
            self.max_robots_crawl_delay = self.DEFAULT_MAX_ROBOTS_CRAWL_DELAY

        # Build exclude sets for scheduling vs emitting. Only SEQUENCE_PARAMS are ever
        # kept -- REORDER_PARAMS (sort/order/dir) stay in UNWANTED_PARAMS in every mode, so
        # a listing is never chased through its sort permutations (issue #58).
        if self.reach_pagination:
            # Visit distinct pagination pages (?page=N reveals new items), but normalize
            # them away when emitting so the stored URL is the canonical listing, not ?page=2.
            self.exclude_params_schedule = set(self.UNWANTED_PARAMS) - self.SEQUENCE_PARAMS
            self.exclude_params_emit = set(self.UNWANTED_PARAMS)
        else:
            # Original behavior (optionally keep/drop pagination everywhere)
            base = set(self.UNWANTED_PARAMS)
            if keep_pagination:
                base -= self.SEQUENCE_PARAMS
            self.exclude_params_schedule = base
            self.exclude_params_emit = base

    def _bind_dedup_state(self):
        """Move the dedup structures INTO `self.state` so they persist across resumable
        sessions (issue #52). Called on FIRST USE, not from a signal -- see the ordering
        note below, which is load-bearing.

        `yoko-corpus` drives one logical crawl as N crawler sessions against a shared
        per-domain JOBDIR. JOBDIR persists Scrapy's frontier and dupefilter, but NOT spider
        attributes -- so `self.seen` came back empty each session and every link found on a
        resumed page was re-scheduled, re-fetching pages earlier sessions had already done.
        Scrapy's dupefilter could not compensate because `_schedule` emits every request with
        `dont_filter=True` (deliberately: this spider does its own normalization-aware dedup,
        which is stricter than a URL fingerprint). Ingest is idempotent so the DATA stayed
        correct, which is why it went unnoticed -- but the crawl budget was spent re-fetching,
        and the waste compounds with size: a 30k-page site is ~13 polite sessions.

        Scrapy's `SpiderState` extension (default-enabled, `EXTENSIONS_BASE` priority 0)
        pickles `self.state` into JOBDIR on close and restores it on open. Binding our
        attributes to the SAME objects held in `self.state` means every mutation is captured
        with no explicit save step.

        ORDERING: this must NOT run from a `spider_opened` handler. `Crawler.crawl()` does
        `_create_spider()` (where a `from_crawler` hook would register ours) and only THEN
        `_apply_settings()` (which loads extensions, registering SpiderState's own
        `spider_opened` handler). Handlers fire in registration order, so ours would run
        first, find no `state` attribute at all, and silently no-op -- reintroducing the very
        bug this fixes while every test still passed. By the time a URL is scheduled the
        engine is running and `self.state` is populated, so first-use binding is
        ordering-independent. With no JOBDIR, `self.state` is a plain dict that starts empty
        each run -- identical behaviour to before.
        """
        if self._state_bound:
            return
        self._state_bound = True
        state = getattr(self, "state", None)
        if state is None:
            # No JOBDIR (SpiderState raises NotConfigured) -> plain in-memory values, as
            # before. But `state` is ALSO missing when SpiderState's pickle.load() raised
            # on a truncated file: it logs and swallows, then at close reopens the file
            # 'wb' and asserts, leaving a 0-byte file that raises EOFError on every later
            # session -- one bad write bricks the domain's resume permanently. Seeding an
            # empty dict here means the next close writes a VALID state file, so a corrupt
            # JOBDIR self-heals at the cost of one re-crawl instead of never recovering.
            settings = getattr(self, "settings", None)  # absent on a bare spider (tests)
            if settings is not None and settings.get("JOBDIR"):
                self.logger.warning(
                    "JOBDIR is set but no spider state was restored (absent or unreadable); "
                    "starting with empty dedup state and rewriting it at close."
                )
                self.state = state = {}
            else:
                return
        # Tolerate a corrupt/foreign persisted shape (a hand-edited or version-skewed
        # JOBDIR): a wrong type is discarded rather than crashing a multi-hour crawl.
        restored_seen = state.get("seen")
        restored_emitted = state.get("emitted")
        restored_refs = state.get("first_referrer")
        state["seen"] = self.seen = restored_seen if isinstance(restored_seen, set) else self.seen
        state["emitted"] = self.emitted = (
            restored_emitted if isinstance(restored_emitted, set) else self.emitted)
        state["first_referrer"] = self.first_referrer = (
            restored_refs if isinstance(restored_refs, dict) else self.first_referrer)
        if self.seen:
            self.logger.info(
                "Resumed dedup state: %d scheduled, %d emitted URLs carried over",
                len(self.seen), len(self.emitted),
            )

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

    # ---------- Faceted-search containment (issue #49) ----------

    @classmethod
    def facet_family(cls, key: str) -> str | None:
        """The family a query param belongs to when it looks like one slot of a
        multi-select facet, else None.

        Two shapes qualify. An INDEXED array param (`f[0]`, `tid[2]`,
        `field_topics[1]`) -> `f[]` / `tid[]` / `field_topics[]`: the index is a slot
        number, not meaning, so `f[0]=a&f[1]=b` and `f[0]=b&f[1]=a` are the same
        selection. Or a bare well-known facet param name (`fq`, `facet`, `filter`).

        Deliberately narrow: an identity param (`?id=5`, `?product=hat`) is NOT a facet
        family, so neither the depth cap nor the order-insensitive dedup below can ever
        collapse two genuinely different product/detail pages onto one key.
        """
        match = cls._FACET_INDEX_RE.match(key)
        if match:
            return f"{match.group(1)}[]"
        return key.lower() if key.lower() in cls.FACET_PARAM_NAMES else None

    @classmethod
    def facet_depth(cls, url: str) -> int:
        """How many facet-shaped params the URL carries -- its filter depth. Non-facet
        params (`?id=5&color=red`) count 0, so only faceted search is ever capped."""
        return sum(1 for key, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)
                   if cls.facet_family(key) is not None)

    def facet_dedup_key(self, url: str) -> str:
        """A scheduling identity that is INSENSITIVE to facet slot order, so the many
        orderings of one facet selection collapse to a single key (issue #49).

        `?f[0]=187&f[1]=79` and `?f[0]=79&f[1]=187` are the same result set under
        different URLs. `w3lib.canonicalize_url` sorts params by NAME, and `f[0]`/`f[1]`
        are different names, so it cannot collapse them -- on naeyc.org that let one
        search page fan out to 1,491 crawled URLs (77.6% of the whole crawl).

        This is a dedup KEY only, never a URL we fetch: the first ordering seen is
        requested with its own real, working URL; later permutations merely hit the key
        in `self.seen` and are dropped. A URL with no facet params returns unchanged, so
        ordinary pages keep their exact identity.
        """
        parsed = urlparse(url)
        families: dict = {}
        plain = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            family = self.facet_family(key)
            if family is None:
                plain.append((key, value))
            else:
                families.setdefault(family, set()).add(value)
        if not families:
            return url
        # Sort values WITHIN each family (slot order carries no meaning) and the families
        # against each other, so every permutation of one selection yields one string.
        flattened = [(family, value)
                     for family in sorted(families)
                     for value in sorted(families[family])]
        return urlunparse(parsed._replace(query=urlencode(sorted(plain) + flattened)))

    def is_asset_url(self, url: str) -> bool:
        path = (urlparse(url).path or "").lower()
        return any(path.endswith(ext) for ext in self.ASSET_EXTENSIONS)

    def is_login_url(self, url: str) -> bool:
        """Detect login/auth URLs by checking path segments against known patterns."""
        path = (urlparse(url).path or "").lower()
        segments = path.split("/")
        return any(seg in self.LOGIN_PATH_SEGMENTS for seg in segments)

    def is_infra_url(self, url: str) -> bool:
        """Detect CMS/CDN infrastructure endpoints (WP REST API, XML-RPC, cron, trackback,
        and Cloudflare's reserved `/cdn-cgi/` path). Machine-only, never site content."""
        path = (urlparse(url).path or "").lower()
        segments = path.split("/")
        return any(seg in self.INFRA_PATH_SEGMENTS for seg in segments)

    def is_robots_disallowed(self, url: str) -> bool:
        """True when the site's robots.txt Disallows this URL for our user-agent group
        (issues #57/#59). False when robots.txt hasn't been parsed yet, the site has none,
        or the match errors -- allow-all, so this only ever ADDS a skip, never a false one."""
        if self._robots is None:
            return False
        try:
            return not self._robots.can_fetch(url, self.ROBOTS_USER_AGENT)
        except Exception:
            return False

    @staticmethod
    def _honored_crawl_delay(requested: float, configured: float, cap: float) -> tuple[float, bool]:
        """The per-request interval to honor for a robots.txt Crawl-delay, and whether it was
        clamped. `max(configured, min(requested, cap))`: never faster than the site asks, never
        below our own floor, never above the cap (which bounds a pathological delay vs the crawl
        budget). Returns (honored_delay, was_clamped)."""
        honored = max(configured, min(requested, cap))
        return honored, requested > cap

    def _apply_crawl_delay(self, requested: float) -> None:
        """Honor a robots.txt Crawl-delay (issue #57): pace this host at the honored interval.
        AutoThrottle floors its adaptive delay at its own `mindelay` (= DOWNLOAD_DELAY), so we
        RAISE that floor to the honored value -- otherwise AutoThrottle would lower the pace
        back below what the site asked. We also bump the live download slot(s) so it takes
        effect on the very next request, not only after the next adjustment. All best-effort:
        a Scrapy-internals shape change must never break the crawl. Note: a large honored delay
        means a fixed-budget crawl finalizes partial (honestly labelled) rather than complete."""
        crawler = getattr(self, "crawler", None)
        if crawler is None:
            return
        configured = crawler.settings.getfloat("DOWNLOAD_DELAY")
        honored, clamped = self._honored_crawl_delay(
            requested, configured, self.max_robots_crawl_delay
        )
        if honored <= configured:
            return  # the site asks for <= our own floor -> nothing to slow down
        # Raise AutoThrottle's floor so its adaptive lowering can't undo the crawl-delay.
        # AutoThrottle is the extension carrying a `mindelay` (its adaptive floor); duck-type
        # on it rather than importing the class, so the hook is trivially testable.
        try:
            for ext in crawler.extensions.middlewares:
                if hasattr(ext, "mindelay"):
                    ext.mindelay = max(float(getattr(ext, "mindelay", 0.0) or 0.0), honored)
        except Exception:
            self.logger.debug("could not raise AutoThrottle floor", exc_info=True)
        # Apply immediately to the live slot(s) (single-host crawl -> one slot).
        try:
            for slot in crawler.engine.downloader.slots.values():
                slot.delay = max(slot.delay, honored)
        except Exception:
            self.logger.debug("could not bump download slot delay", exc_info=True)
        self._stat("robots_crawl_delay_applied")
        self.logger.info(
            "Honoring robots.txt Crawl-delay for %s: pacing at %.1fs/request "
            "(site asked %.1fs%s)",
            self.base_domain, honored, requested,
            f"; clamped to the {self.max_robots_crawl_delay:.0f}s cap" if clamped else "",
        )

    # ---------- Entry points ----------

    def _stat(self, name, count=1):
        """Bump a crawl stat, tolerating a spider built without a crawler (unit tests)."""
        crawler = getattr(self, "crawler", None)
        stats = getattr(crawler, "stats", None) if crawler else None
        if stats is not None:
            stats.inc_value(name, count)

    def _seed_requests(self):
        """The crawl's only seed: robots.txt. The start URL(s) are emitted from
        `parse_robots` (or `robots_failed`), NOT from here.

        Ordering is load-bearing (issue #76). The Disallow gate lives in `_schedule` and
        is a no-op while `self._robots` is None, so any page scheduled before robots.txt
        is parsed escapes it permanently -- nothing re-checks a queued request at download
        time. Seeding the start URL alongside robots.txt made that a RACE: on gastro.org
        (`User-agent: * / Disallow: /`) the homepage came back first, its 56 links were all
        scheduled un-gated, and the crawl closed `finished` with 57 pages against a 2,347-URL
        sitemap -- reported as a complete inventory of a "simple" site. Fetching robots.txt
        as a prerequisite removes the race by construction rather than narrowing it.

        Every seed is counted (`seeding/seeds_emitted`) so a crawl can PROVE this ran.
        That is the tripwire for the bug class that killed it once already: Scrapy renamed
        the seeding entry point, our method became unreachable, and nothing failed -- no
        exception, no test, no log line. A crawl seeded by Scrapy's default instead of this
        method reports 0 here, which `stats_extension` turns into a loud error."""
        self._stat("seeding/seeds_emitted")
        yield scrapy.Request(
            urljoin(self.start_urls[0], "/robots.txt"),
            callback=self.parse_robots,
            errback=self.robots_failed,
            dont_filter=True,
        )

    def _start_url_requests(self):
        """Emit the start URL(s), exactly once per crawl. Called once robots.txt has
        resolved -- parsed, missing (404), unreachable, or redirected somewhere we won't
        follow -- so the Disallow gate is in its final state before any page is scheduled.

        NOT routed through `_schedule`: a start URL is the operator's explicit instruction
        and the crawl's only entry point. Dropping it would leave a crawl with nothing to
        do and no row explaining why; a robots-disallowed start URL is reported honestly by
        the crawl coming back empty (and, once #74 lands, by the skip counts)."""
        if self._start_urls_emitted:
            return
        self._start_urls_emitted = True
        for url in self.start_urls:
            self._stat("seeding/seeds_emitted")
            yield scrapy.Request(url, callback=self.parse, dont_filter=True)

    def robots_failed(self, failure):
        """robots.txt never arrived -- DNS failure, refused connection, timeout (issue #76).

        Scrapy routes transport failures to an errback, not the callback, so without this
        the start URLs would never be emitted and a momentary network fault would silently
        produce a ZERO-page crawl. Allow-all is the same posture as a site with no
        robots.txt: we could not read a "no", so we do not invent one."""
        self._stat("seeding/robots_failed")
        self.logger.warning(
            "robots.txt could not be fetched (%s) -- proceeding allow-all, as for a site "
            "with no robots.txt. Crawl-delay and Disallow rules are unknown for this crawl.",
            failure.value if hasattr(failure, "value") else failure,
        )
        yield from self._start_url_requests()

    async def start(self):
        """Seed the crawl (Scrapy >= 2.13's entry point).

        REQUIRED, not optional (issue #52 review). Scrapy 2.13 replaced `start_requests()`
        with `async def start()`, and 2.17 removed the base `Spider.start_requests` and every
        call site -- so on the installed Scrapy our `start_requests` below was DEAD CODE and
        the default `Spider.start()` (start_urls only, no robots.txt) was seeding instead.
        Verified by instrumenting a real Crawler: `start_requests()` never ran and no crawl in
        the archive ever fetched robots.txt.

        The silent regression that caused: robots.txt -> sitemap discovery never ran, so the
        crawler was link-following only and never saw sitemap-only or orphaned pages.
        `requirements.txt` pinned `scrapy>=2.11` with no upper bound, so an ordinary dependency
        upgrade broke it without a single test failing.
        """
        for request in self._seed_requests():
            yield request

    def start_requests(self):
        """Seed the crawl on Scrapy < 2.13, where `start()` does not exist. Kept so the
        pinned range stays crawlable from either entry point; `start` is what runs today."""
        return self._seed_requests()

    # ---------- Robots & sitemaps ----------

    def parse_robots(self, response):
        # Record robots fetch. Counted so a crawl can show sitemap discovery actually
        # happened -- a site with no robots.txt still 404s here, so a ZERO means the seed
        # never went out, not that the site lacks one.
        self._stat("seeding/robots_fetched")
        yield from self._emit_row(response)

        # One-hop redirect follow -- only on-domain (issue corpus#71). robots.txt should redirect
        # within the site (http->https, apex->www); an off-domain hop is a handoff to another site,
        # not our robots, so don't fetch it as ours.
        #
        # Every exit from this branch must resolve seeding (issue #76): the followed hop
        # re-enters parse_robots and seeds there, but a redirect with no Location, or one
        # pointing off-domain, ends the robots chain here -- and if we returned without
        # seeding, the crawl would fetch nothing at all.
        if response.status in self.REDIRECT_STATUSES:
            target = response.headers.get("Location")
            if target:
                tgt = response.urljoin(target.decode("latin-1"))
                if self.is_internal(tgt):
                    yield scrapy.Request(
                        tgt, callback=self.parse_robots, errback=self.robots_failed,
                        dont_filter=True,
                    )
                    return
            yield from self._start_url_requests()
            return

        # Parse robots.txt rules from the real body (issues #57/#59): Disallow gates scheduling
        # (is_robots_disallowed) and a Crawl-delay paces the host. Best-effort -- a malformed or
        # non-text robots.txt leaves rules unset (allow-all), never breaking the crawl.
        if response.status == 200 and isinstance(response, TextResponse):
            try:
                self._robots = Protego.parse(response.text)
                delay = self._robots.crawl_delay(self.ROBOTS_USER_AGENT)
                if delay:
                    self._apply_crawl_delay(float(delay))
            except Exception:
                self.logger.debug("robots.txt parse failed for %s", response.url, exc_info=True)

        # Discover sitemaps -- only on-domain (a robots.txt can list a third-party sitemap URL).
        # Guarded on TextResponse: a binary/non-text body has no `.text`, and since seeding
        # now hangs off this callback (issue #76) an AttributeError here would take the whole
        # crawl down, not just sitemap discovery.
        if isinstance(response, TextResponse):
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm_url = line.split(":", 1)[1].strip()
                    if sm_url and self.is_internal(sm_url):
                        yield scrapy.Request(sm_url, callback=self.parse_sitemap, dont_filter=True)

        # Rules are now in their final state -- seed the crawl (issue #76). Emitted LAST so
        # `self._robots` and any Crawl-delay pacing are already applied to every page URL
        # that follows, including the start URL itself.
        yield from self._start_url_requests()

    def parse_sitemap(self, response):
        # Record sitemap fetch. `sitemaps_fetched` vs `seeding/robots_fetched` distinguishes
        # "we asked for robots.txt and the site listed no sitemap" from "we never asked".
        self._stat("seeding/sitemaps_fetched")
        yield from self._emit_row(response)

        # One-hop redirect follow -- only on-domain (issue corpus#71): an off-domain sitemap redirect
        # points at another site's sitemap, not ours.
        if response.status in self.REDIRECT_STATUSES:
            target = response.headers.get("Location")
            if target:
                tgt = response.urljoin(target.decode("latin-1"))
                if self.is_internal(tgt):
                    yield scrapy.Request(tgt, callback=self.parse_sitemap)
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

        # An origin-generated 403 (not a CF wall) -- typically member-restricted content.
        # Counted for observability (the scale of gated content on a site we didn't build),
        # NOT treated as a block. The row is already emitted; keep parsing so any real links
        # (e.g. a "log in to view" link) are still followed.
        if response.status == 403:
            self.crawler.stats.inc_value("origin_forbidden_count")

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

        # Page-level robots directive: <meta name="robots" content="...nofollow..."> (or the
        # `none` shorthand) means the site asks crawlers not to follow this page's links.
        # We obey it -- the row is still emitted (the page is real content), we just don't
        # schedule what it links out to. This is a stated-intent signal, the same class as
        # rel="nofollow" and robots.txt; honoring it keeps us out of link mazes by design.
        if self._page_meta_nofollow(response):
            self.crawler.stats.inc_value("meta_nofollow_pages")
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
            # rel="nofollow" on the link itself (also "ugc"/"sponsored", which carry the
            # same do-not-follow intent). Cloudflare's AI Labyrinth links are injected as
            # invisible nofollow anchors, so honoring this alone keeps us out of the trap --
            # `cdn-cgi` filtering above is the belt to this page-level suspenders.
            rel_tokens = (sel.attrib.get("rel") or "").lower().split()
            if {"nofollow", "ugc", "sponsored"} & set(rel_tokens):
                self.crawler.stats.inc_value("nofollow_links_skipped")
                continue
            full_url = response.urljoin(href)
            if self.is_internal(full_url):
                yield from self._schedule(full_url, referrer_emit=current_emit)

    # ---------- Helpers ----------

    def _page_meta_nofollow(self, response) -> bool:
        """True when the page carries a <meta name="robots" content="..."> directive asking
        crawlers not to follow its links -- an explicit `nofollow`, or the `none` shorthand
        (which means noindex,nofollow). Name and content are matched case-insensitively and
        content is tokenized on commas/whitespace, so `content="NoIndex, NoFollow"` matches.
        Best-effort: any parse error means "no directive", never a crash."""
        try:
            contents = response.xpath("//meta[@name and @content]/@content").getall()
            names = response.xpath("//meta[@name and @content]/@name").getall()
        except Exception:
            return False
        for name, content in zip(names, contents):
            if name.strip().lower() != "robots":
                continue
            tokens = {t for t in re.split(r"[,\s]+", content.lower()) if t}
            if "nofollow" in tokens or "none" in tokens:
                return True
        return False

    # Response headers that only the ORIGIN app sets, never a Cloudflare-generated
    # challenge/block page. Their presence on a 403 means the origin produced it (an
    # app-level Forbidden -- typically member-restricted content we DO want inventoried),
    # not a CF bot wall. Kept lowercase; matched against lowercased header names.
    _ORIGIN_HEADER_FINGERPRINTS = (
        "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version",
        "x-generator", "x-drupal-cache", "x-drupal-dynamic-cache",
        "x-litespeed-cache", "x-redirect-by",
    )
    # Set-Cookie names an origin stack sets (session/affinity), as opposed to Cloudflare's
    # own `cf_*` / `__cf_*` cookies. A non-CF Set-Cookie on a 403 is an origin fingerprint.
    _CF_COOKIE_PREFIXES = ("cf_", "__cf")

    def _has_origin_fingerprint(self, response) -> bool:
        """True when a response carries a header only the origin application sets -- proof
        it was generated by the origin and merely proxied through Cloudflare, not by CF
        itself. Used to keep a proxied origin 403 (member-restricted content) from being
        miscounted as a bot-wall challenge on Cloudflare-fronted sites (where every response
        also carries `cf-ray`/`server: cloudflare`)."""
        headers = response.headers
        if any(headers.get(h) for h in self._ORIGIN_HEADER_FINGERPRINTS):
            return True
        # A Set-Cookie whose name isn't one of Cloudflare's own cookies is the origin's.
        for raw in headers.getlist("Set-Cookie"):
            name = raw.decode("latin-1").split("=", 1)[0].strip().lower()
            if name and not name.startswith(self._CF_COOKIE_PREFIXES):
                return True
        return False

    def _is_waf_challenge(self, response) -> bool:
        """True when a response is a Cloudflare-generated bot-wall challenge or block page,
        not a real page. Keyed on a challenge status (403/429/503) PLUS a Cloudflare
        signal, while excluding origin-generated 403s so member-restricted content stays in
        the inventory rather than being mislabeled as a bot wall:
          - `cf-mitigated` header — Cloudflare stamps it ONLY on responses IT generated as a
            challenge/block (INCOSE's `/setdb-login/` returns `403 cf-mitigated: challenge`
            with a "Just a moment..." body). This is the reliable, high-precision signal.
          - Fallback: a `cf-ray` header AND `server: cloudflare` at a challenge status, BUT
            only when the response has NO origin fingerprint. On a CF-fronted site every
            response carries cf-ray/server, including a proxied origin 403 -- the
            origin-fingerprint guard is what keeps those (restricted content) out of the
            challenge bucket.
        Conservative on purpose: a false positive only costs us one page's content/links
        (already unreadable at 403), while a false negative re-introduces the wall-page
        pollution this guards against."""
        if response.status not in self.WAF_CHALLENGE_STATUSES:
            return False
        headers = response.headers
        if headers.get("cf-mitigated"):
            return True
        server = (headers.get("Server") or b"").decode("latin-1").lower()
        if not (bool(headers.get("cf-ray")) and "cloudflare" in server):
            return False
        # CF-fronted, challenge status, no cf-mitigated: a real CF wall (e.g. a legacy
        # challenge) has no origin headers; a proxied origin 403 does -- treat that as
        # origin content, not a wall.
        return not self._has_origin_fingerprint(response)

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
                # Third-party integrations inject via <script src> anywhere on the page too
                # (issue #28) -- the iframe blind spot's bigger sibling.
                scripts = script_signals(
                    response.body, self.benign_script_hosts, self.self_hosts
                )
                # Interactive JS components are page-wide too (issue #12); image sliders/
                # carousels are the slider subset of that, counted page-wide (issue #25).
                components = component_signals(result.body_subtree)
                sliders = slider_signals(result.body_subtree)
                fields = empty_enrichment()
                fields.update(counts)
                fields["content_hash"] = content_hash(result.normalized_text)
                # Structural fingerprint over the FULL body -- clusters into templates (#36).
                # Uses body_subtree, not the located content region: the located subtree shifts
                # with content length (trafilatura), which would split same-template pages; the
                # body is content-stable, and shared chrome is constant across all pages so it
                # doesn't blur distinct templates.
                fields["structure_hash"] = structure_hash(result.body_subtree)
                fields["main_content_extracted"] = result.main_content_extracted
                fields["embed_count_nonbenign"] = signals["embed_count_nonbenign"]
                fields["component_count"] = components["component_count"]
                fields["slider_count"] = sliders["slider_count"]
                fields["iframe_hosts"] = signals["iframe_hosts"]
                fields["script_embed_count_nonbenign"] = scripts["script_embed_count_nonbenign"]
                fields["script_hosts"] = scripts["script_hosts"]
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

        # CSV can't hold a real array; JSON-encode the list fields so they round-trip.
        # jsonlines keeps the native arrays (what yoko-corpus consumes).
        if self.output_format == "csv":
            fields["iframe_hosts"] = json.dumps(fields["iframe_hosts"])
            fields["script_hosts"] = json.dumps(fields["script_hosts"])
            fields["internal_link_targets"] = json.dumps(fields["internal_link_targets"])
            fields["external_link_hosts"] = json.dumps(fields["external_link_hosts"])

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
        self._bind_dedup_state()  # first-use restore from JOBDIR (issue #52)
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

        # First referrer, if we have it (prefer what we captured at schedule time).
        # Looked up by the same facet-dedup key `_schedule` stored it under (issue #49),
        # so a facet URL still resolves its referrer.
        current_schedule = self.facet_dedup_key(
            self.normalize_url(response.url, exclude_params=self.exclude_params_schedule)
        )
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
            # Empty on a real (fetched) page; only a deliberately-skipped URL carries a
            # reason (see _skip_row). Present on every row so the CSV/JSONL shape is stable.
            "skip_reason": "",
        }
        row.update(self._enrichment(response))
        yield row

    def _skip_row(self, url: str, reason: str, referrer_emit: str | None) -> dict:
        """A feed row for a URL we deliberately did NOT fetch (issue #43): the URL, a zero
        status (never fetched), the skip `reason`, and the referrer that linked to it. The
        corpus routes any row with a non-empty `skip_reason` to its excluded_urls store, so
        this surfaces as coverage ("auth-gated areas we didn't crawl") without ever being
        counted, analyzed, or exported as a crawled page."""
        return {
            "url": url,
            "status": 0,
            "last_modified": "",
            "redirected_to": "",
            "referrer": referrer_emit or "",
            "skip_reason": reason,
        }

    def _schedule(self, url, *, referrer_emit: str | None = None):
        """
        Normalize with schedule-mode (pagination retained when reach_pagination=1),
        de-dup, and enqueue the next request. Record the first referrer seen.
        """
        self._bind_dedup_state()  # first-use restore from JOBDIR (issue #52)
        if not self.is_internal(url):
            return
        normalized = self.normalize_url(url, exclude_params=self.exclude_params_schedule)
        # Faceted search (issue #49): dedup on a slot-order-insensitive key so the many
        # orderings of one filter selection collapse, and drop selections deeper than the
        # cap -- those are duplicate views of a result set, not pages a redesign builds.
        # Checked BEFORE `seen` so a capped URL is never recorded as visited.
        if self.facet_depth(normalized) > self.max_facet_depth:
            self.crawler.stats.inc_value("facet_urls_skipped")
            self.logger.debug("Skipping deep facet URL: %s", normalized)
            return
        seen_key = self.facet_dedup_key(normalized)
        if seen_key in self.seen:
            return

        # robots.txt Disallow (issues #57/#59): the site asked crawlers not to fetch this
        # path. Obey it -- this is what keeps us out of `Disallow: /search/` facet traps
        # (naeyc: 693 permutations, 38% of the crawl) and, more broadly, off anything the
        # site marks off-limits. No-op until robots.txt is parsed / for a site without one.
        if self.is_robots_disallowed(normalized):
            self.seen.add(seen_key)
            self.crawler.stats.inc_value("robots_disallowed_skipped")
            self.logger.debug("Skipping robots-disallowed URL: %s", normalized)
            return

        if self.is_login_url(normalized):
            self.seen.add(seen_key)
            self.crawler.stats.inc_value("login_urls_skipped")
            self.logger.debug("Skipping login/auth URL: %s", normalized)
            # Emit a skip record (issue #43): we deliberately don't fetch auth/login-gated
            # URLs, but "we found a members-only area and didn't crawl it" is a real
            # migration-scoping signal. The corpus routes rows with a `skip_reason` to its
            # excluded_urls store (never page_versions), so this never counts as a page.
            yield self._skip_row(normalized, "login_gated", referrer_emit)
            return

        if self.is_infra_url(normalized):
            self.seen.add(seen_key)
            self.crawler.stats.inc_value("infra_urls_skipped")
            self.logger.debug("Skipping infrastructure URL: %s", normalized)
            return

        if self.is_asset_url(normalized):
            self.seen.add(seen_key)
            if referrer_emit:
                self.first_referrer.setdefault(seen_key, referrer_emit)
            self.logger.info("Fetching headers for asset URL: %s", normalized)
            yield scrapy.Request(
                normalized,
                callback=self.parse_asset,
                method="HEAD",
                dont_filter=True,
            )
            return

        # Store first referrer for this scheduled target (facet-dedup key, emit-norm value)
        if referrer_emit:
            self.first_referrer.setdefault(seen_key, referrer_emit)

        self.seen.add(seen_key)
        # Request the URL as NORMALIZED, not as the dedup key: the key reorders facet
        # slots into a canonical form that the site may not serve. The first ordering we
        # saw is a real, working URL.
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
