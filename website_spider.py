from __future__ import annotations

import json
import math
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

# Bounds for the robots.txt fetch budget (issue #92). MODULE-level, deliberately, and this
# is load-bearing rather than stylistic: Scrapy assigns `-a` spider arguments directly into
# `self.__dict__`, so a floor read as `self.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT` is shadowable --
# `-a DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT=1` was measured honouring a 5s budget in review, which
# is precisely the allow-all the floor exists to prevent. A module constant is out of reach
# of that mechanism.
#
# FLOOR (60s): below this, a slow-but-real robots.txt times out into `robots_failed`, which
# proceeds ALLOW-ALL -- the knob may only ever raise.
#
# CEILING (600s, 10x the default): robots.txt is the crawl's ONLY seed (#76), so nothing is
# fetched until it resolves and one fetch costs (ROBOTS_MAX_RETRY_TIMES + 1) x the budget --
# 1800s at the ceiling, a quarter of the 7200s CLOSESPIDER_TIMEOUT, with the redirect-hop and
# sitemap-probe multipliers on top of that. Past the ceiling the stall outruns job_manager's
# 7500s watchdog, which SIGKILLs the subprocess into `status: failed` with a NULL
# failure_reason and deletes the JOBDIR. That is strictly worse than the allow-all the floor
# guards against, so the raise direction needs a bound too -- the sibling crawl-delay knob has
# always had one (`min(asked, cap)`), and this one lacking it was the real asymmetry (#92
# review).
_ROBOTS_TIMEOUT_FLOOR = 60
_ROBOTS_TIMEOUT_CEILING = 600

# Mirrors job_manager._WATCHDOG_TIMEOUT. Duplicated rather than imported: the spider runs in a
# subprocess that must not import the API process's modules. Only ever used in a log message.
_JOB_WATCHDOG_TIMEOUT = 7500

# Zero/empty enrichment defaults come from content_extractor.empty_enrichment()
# (the single source of truth for field names). content_text is handled
# separately: present only when --emit-content is set.


def _depth_reset_supported() -> bool:
    """Does the INSTALLED Scrapy honour the `depth_reset` meta key (added in 2.18)?

    Feature-detected against the real DepthMiddleware rather than trusted from a version
    string, because the failure it guards is precisely a fix that no-ops in silence (#81):
    on an older Scrapy the key is unrecognised, the start URL stays at depth 1, and nothing
    logs, raises, or fails a test. Detecting it by BEHAVIOUR also means a backport or a
    rename cannot make this lie. Any exception means "assume unsupported" -- a spurious
    warning is a far cheaper mistake than a silent off-by-one."""
    try:
        from scrapy.http import Request as _R, Response as _Rs
        from scrapy.spidermiddlewares.depth import DepthMiddleware

        class _NullStats:
            def inc_value(self, *a, **k): pass
            def max_value(self, *a, **k): pass

        parent = _Rs("https://example.invalid/", request=_R("https://example.invalid/"))
        parent.meta["depth"] = 7
        child = _R("https://example.invalid/child", meta={"depth_reset": True})
        out = DepthMiddleware(0, _NullStats(), False, 1).get_processed_request(child, parent)
        return out is not None and out.meta.get("depth") == 0
    except Exception:
        return False


_DEPTH_RESET_SUPPORTED = _depth_reset_supported()

class WebsiteSpider(scrapy.Spider):
    """
    Internal crawler that:
      - Treats base domain and www as internal; optional flag to allow all subdomains
      - Normalizes & de-duplicates URLs (drops fragments, strips junk params)
      - Records per-URL HTTP status, single-hop redirect target, and first referrer
      - Seeds from robots.txt → sitemap(s); the start URL is emitted only after
        robots.txt resolves, so the Disallow gate is never open (issue #76)
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
        # One platform observation per crawl (corpus #112) -- the platform is a site
        # property, so re-deriving it per page is waste.
        self._platform_recorded = False
        self._max_delay_requested = os.environ.get("YOKO_CRAWL_MAX_ROBOTS_DELAY")
        self._max_delay_disposition = (
            "default" if self._max_delay_requested is None else "honoured")
        try:
            parsed_delay = float(
                os.environ.get("YOKO_CRAWL_MAX_ROBOTS_DELAY", self.DEFAULT_MAX_ROBOTS_CRAWL_DELAY)
            )
            # `float()` accepts "inf"/"nan"/"1e400", and a cap of infinity is not a cap -- it
            # honours any Crawl-delay a site asks for, which is what this knob exists to bound.
            # Flagged as pre-existing during the #92 review and harmless while the value stayed
            # inside the process; #99 is what carries it OUT, and that turned it into a P1.
            #
            # `json.dump` writes bare `Infinity`/`NaN`, which is not valid JSON. Starlette's
            # JSONResponse renders with allow_nan=False, so `GET /crawl/{id}` raises, and
            # yoko-corpus's poll loop calls raise_for_status() uncaught -- the first poll aborts
            # the ingest while the spider crawls happily on. One env typo, every crawl on that
            # host un-ingestable. Traced end to end in review.
            #
            # Non-positive is refused for the same reason it is refused everywhere else here:
            # `0` reads as "no cap" in exactly the direction that removes the bound.
            if not math.isfinite(parsed_delay) or parsed_delay <= 0:
                raise ValueError(f"not a usable cap: {parsed_delay!r}")
            self.max_robots_crawl_delay = parsed_delay
        except (TypeError, ValueError, OverflowError):
            # Falls back SILENTLY, unlike its sibling -- pre-existing, kept because the
            # consequence is mild (the cap reverts to 10s). The disposition records it now, so
            # an operator whose value did nothing can SEE that rather than guess (#99).
            self._max_delay_disposition = "invalid"
            self.max_robots_crawl_delay = self.DEFAULT_MAX_ROBOTS_CRAWL_DELAY
        # Operator override for the robots.txt fetch budget (issue #92). Env-tunable like the
        # knob above, but do NOT read the two as symmetric -- they are bounded in opposite
        # directions and the difference is the point. The crawl-delay knob is a float CAP: the
        # site asks, we honour at most the cap. This one is an int FLOOR: the operator asks,
        # we honour at least the default. A shorter robots.txt bound silently converts "this
        # site was slow" into "this site has no robots.txt" -- `robots_failed` proceeds
        # ALLOW-ALL -- so tuning it down does not make crawls snappier, it makes them crawl
        # sites that said Disallow. Below-floor values are refused and logged; the knob exists
        # for the opposite case, a genuinely slow host worth waiting longer for, without a
        # redeploy. It is capped too (`_ROBOTS_TIMEOUT_CEILING`), because an unbounded raise
        # stalls past the watchdog into a SIGKILLed `failed` crawl -- worse than the allow-all
        # the floor prevents (#92 review). Like `_apply_crawl_delay`, every clamp is logged.
        self.robots_download_timeout = self._resolve_robots_timeout()

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

    # Response headers and head tags that identify the CMS behind a site. Observation only --
    # the crawler records what it saw and yoko-corpus decides what platform that means, the
    # same split as the blocking/restriction counts.
    #
    # Why this exists (corpus #112): platform detection ran on the URL SPACE, looking for
    # `/wp-content/`, `/wp-json/`, `/wp-admin/`. But `wp-json` is in INFRA_PATH_SEGMENTS and
    # `wp-admin` in LOGIN_PATH_SEGMENTS -- we are built never to schedule them -- and
    # `/wp-content/` only ever appears as an ASSET row, which needs an <a href> to a file;
    # images are <img src> and are never scheduled. So a WordPress site with pretty
    # permalinks and no linked PDFs scores ZERO WordPress markers. The better configured the
    # site, the less likely we identified it. gastro.org (WordPress, WP Engine, Yoast) was
    # reported as "Custom / other", which flips the headline from a content migration to a
    # from-scratch rebuild -- the more expensive story.
    #
    # Every signal below is available on the FIRST response of any crawl.
    _PLATFORM_HEADER_SIGNALS = (
        # (header, substring to match or None for "present at all", token)
        ("link", "api.w.org", "wp-rest-api-link"),
        ("x-pingback", None, "x-pingback"),
        ("x-generator", None, "x-generator"),
        ("x-powered-by", None, "x-powered-by"),
        ("x-drupal-cache", None, "x-drupal-cache"),
        ("x-drupal-dynamic-cache", None, "x-drupal-cache"),
        ("x-shopify-stage", None, "x-shopify"),
    )
    # Cap on how much header/meta text we keep per signal: enough to identify a platform and
    # its version, short enough that a hostile or verbose header can't bloat the status file.
    _PLATFORM_VALUE_MAXLEN = 120
    # How many generator metas to keep. Enough to see core plus the plugins that matter,
    # bounded so a page emitting dozens can't bloat the status file.
    _PLATFORM_MAX_GENERATORS = 5

    def _record_platform_signals(self, response) -> None:
        """Record CMS fingerprints from the first successful HTML response (corpus #112).

        Only the first: the platform is a property of the SITE, so one good observation is
        the whole answer and re-deriving it per page would be waste. Best-effort throughout
        -- a platform hint is a nice-to-have and must never cost a crawl."""
        if self._platform_recorded:
            return
        stats = getattr(getattr(self, "crawler", None), "stats", None)
        if stats is None or response.status != 200 or not isinstance(response, TextResponse):
            return
        try:
            signals = {}
            identifying = False
            for header, needle, token in self._PLATFORM_HEADER_SIGNALS:
                raw = response.headers.getlist(header.encode())
                if not raw:
                    continue
                value = b" ".join(raw).decode("latin-1")
                if needle and needle not in value.lower():
                    continue
                signals[token] = value[: self._PLATFORM_VALUE_MAXLEN]
                # A needle-matched header proved WHAT it is; a bare one (x-powered-by:
                # PHP/8.2) merely exists.
                identifying = identifying or bool(needle)
            # <meta name="generator" content="WordPress 6.9.4">. XPath with a lowercase
            # translate because CSS attribute-VALUE matching is case-sensitive in cssselect
            # and Drupal core emits `name="Generator"` -- a `[name="generator"]` selector is
            # silently blind to it.
            # ALL generator metas, not just the first (corpus #115). A page carries several:
            # WordPress core emits one and plugins APPEND their own -- Elementor, WooCommerce,
            # WP Rocket, WPML, AIOSEO. They do not overwrite core's. Taking only the first
            # meant that on a site which strips `wp_generator` (a common hardening/SEO
            # `remove_action('wp_head','wp_generator')`) we captured whichever plugin
            # happened to be first and threw the rest away -- so whether the platform was
            # identifiable came down to plugin load order.
            gens = response.xpath(
                "//meta[translate(@name, 'GENRATO', 'genrato')='generator']/@content"
            ).getall()
            values = []
            for gen in gens:
                gen = (gen or "").strip()
                if gen:
                    values.append(gen[: self._PLATFORM_VALUE_MAXLEN])
                if len(values) >= self._PLATFORM_MAX_GENERATORS:
                    break
            if values:
                signals["generator"] = "; ".join(values)
                identifying = True
            # `rel` is a space-separated TOKEN list, so `~=` not `=`:
            # rel="https://api.w.org/ alternate" is valid and must still match.
            if response.css('link[rel~="https://api.w.org/"]'):
                signals["wp-rest-api-link"] = "https://api.w.org/"
                identifying = True
            if not identifying:
                # Nothing here NAMES a platform -- a bare `x-powered-by: PHP/8.2` is not an
                # answer. Record nothing and keep looking: latching on it would stop us
                # inspecting the interior pages, and a WordPress site whose homepage is
                # served from an edge cache that strips the REST link and the generator
                # carries both on its uncached pages. Latching there would leave the site
                # reported as "Custom / other" -- issue #112, un-fixed, on a site that was
                # telling us the answer one page later.
                return
            # Flag set AFTER the write: if set_value raised we would be latched but
            # unrecorded for the whole session.
            stats.set_value("platform_signals", signals)
            self._platform_recorded = True
            self.logger.info("Platform signals for %s: %s", self.base_domain, sorted(signals))
        except Exception:
            self.logger.debug("could not record platform signals", exc_info=True)

    def _record_robots_scope(self) -> None:
        """Record whether robots.txt disallows the SITE ROOT for our user-agent group
        (issue #74 review).

        The skip COUNTER is a poor proxy for "the site withheld itself": it only counts
        URLs that reached `_schedule`, i.e. ones we found a link to or a sitemap entry for.
        A site with `Disallow: /` and no `Sitemap:` line yields skips equal to the
        homepage's link count -- and one whose homepage is a JS shell or carries
        `meta robots nofollow` yields almost none, which is the most withheld a site can be
        and the least visible in the count.

        This asks the direct question instead, answered from the rules themselves: does our
        group's robots.txt disallow the root? It needs no threshold, cannot be fooled by
        what we did or did not discover, and is exactly the gastro.org shape."""
        stats = getattr(getattr(self, "crawler", None), "stats", None)
        if stats is None or self._robots is None:
            return
        try:
            root = urljoin(self.start_urls[0], "/")
            stats.set_value(
                "robots_root_disallowed", 1 if self.is_robots_disallowed(root) else 0
            )
        except Exception:
            self.logger.debug("could not record robots scope", exc_info=True)

    def _persist_robots_body(self, body: str) -> None:
        """Carry robots.txt across resumable sessions (issue #76). Best-effort: a crawl with
        no JOBDIR has no state to write to, and failing to persist must never break a
        running crawl -- it only costs the resumed session its gate, as before."""
        try:
            self._bind_dedup_state()
            state = getattr(self, "state", None)
            if state is not None:
                state["robots_body"] = body
        except Exception:
            self.logger.debug("Could not persist robots.txt to spider state", exc_info=True)

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
        # Restore the robots.txt rules too (issue #76). Without this the Disallow gate is
        # OPEN at the start of every resumed session: `self._robots` is a plain attribute,
        # JOBDIR restores the frontier but not spider attributes, and yoko-corpus runs one
        # logical crawl as N sessions. Session 2 pops the robots.txt seed first, but the
        # downloader immediately fills the other CONCURRENT_REQUESTS-1 slots from the
        # restored frontier -- and any of those returning before robots.txt lands schedules
        # its links un-gated. That is #76's exact bug surviving on the path corpus actually
        # uses, which is why "removed by construction" needs this to be true.
        #
        # The BODY is persisted, not the Protego matcher: a plain string is guaranteed
        # picklable across versions, and re-parsing is cheap and once per session.
        restored_robots = state.get("robots_body")
        if self._robots is None and isinstance(restored_robots, str):
            try:
                self._robots = Protego.parse(restored_robots)
                self.logger.info("Resumed robots.txt rules from the previous session.")
            except Exception:
                self.logger.debug("Could not re-parse persisted robots.txt", exc_info=True)
        if self.seen:
            self.logger.info(
                "Resumed dedup state: %d scheduled, %d emitted URLs carried over",
                len(self.seen), len(self.emitted),
            )

    # ---------- URL helpers ----------

    # The only schemes a web crawl may fetch. Everything else is refused by `is_internal`
    # BEFORE the host is even considered (issue #89).
    #
    # This is a scheme confusion bug, and the host check cannot catch it: `is_internal`
    # compared only the hostname, so `file://<the-crawled-domain>/etc/passwd` matched the
    # domain and was accepted. Scrapy registers handlers for `file`, `ftp`, `s3` and `data`
    # in DOWNLOAD_HANDLERS_BASE, and `FileDownloadHandler` resolves the URL through
    # `w3lib.url.file_uri_to_path`, which DISCARDS the host and reads the bare path -- so a
    # site we crawl could make the crawler read a local file on the crawl host and carry it
    # into the JOBDIR state and the NDJSON rows. `SsrfGuardMiddleware` does not close this
    # either: it resolves the hostname and blocks reserved ADDRESSES, and the crawled
    # domain resolves publicly, so the request passes it and reaches the local-file handler.
    #
    # FIVE intake paths take a value the REMOTE SERVER controls and had `is_internal` as
    # their only validation: a `Location:` on a robots.txt redirect, a `Sitemap:` line, a
    # sitemap's `<loc>`, and the `Location:` on a page redirect and on an asset redirect
    # (both of which reach `_schedule`, whose only gate is this one).
    #
    # And the `<a href>` path was NOT already safe, contrary to the first version of this
    # comment: `_NONNAV_SCHEMES` lists `file:`/`ftp:`/`data:` but NOT `s3:`, so
    # `is_navigational_href("s3://<domain>/x")` returned True and `s3` is in Scrapy's
    # DOWNLOAD_HANDLERS_BASE. (`s3:` has since been added there too, but that is
    # defence-in-depth -- this gate is what actually closes it.)
    #
    # Which is the argument for fixing it HERE rather than at each call site: the first
    # count of the call sites was wrong in both directions, and a gate that fails closed
    # does not depend on getting that count right.
    _FETCHABLE_SCHEMES = frozenset({"http", "https"})

    @staticmethod
    def _resolve_site_url(response, raw):
        """Resolve a site-supplied URL against `response`, or None if it cannot be resolved.

        `urljoin` RAISES ValueError on a malformed netloc, exactly like `urlparse` -- so
        making `is_internal` fail closed (#89 review) was not enough on its own: adding the
        urljoin in front of it just moved the same raise one line earlier, still aborting a
        loop over a site-supplied list and still costing every later entry. Both halves have
        to be non-raising for the "one bad value does not sink the rest" property to hold."""
        try:
            return response.urljoin((raw or "").strip()) or None
        except ValueError:
            return None

    def is_internal(self, url: str) -> bool:
        """Accept bare domain or www; optionally allow any subdomain of base domain.

        Non-http(s) schemes are refused outright -- see `_FETCHABLE_SCHEMES` (issue #89).
        A scheme-LESS URL is refused by the same term, so callers that take a raw
        site-supplied value must `urljoin` it against the response FIRST -- which is what
        the `Sitemap:` and `<loc>` readers now do. urljoin resolves `/sitemap.xml` and
        `//host/sitemap.xml` against an http(s) base while leaving an absolute hostile
        scheme untouched for this gate to refuse, so it recovers coverage without laundering
        anything (`//evil.test/x` becomes `https://evil.test/x` and then fails the host
        compare). An earlier version of this docstring called refusing scheme-less URLs "a
        fix rather than a restriction" -- half true, and the missing half was a silent
        coverage loss on any site whose robots.txt names its sitemap relatively.

        NEVER RAISES. `urlparse` raises ValueError on a malformed netloc (`http://[::1/x` ->
        "Invalid IPv6 URL"), and this gate is called mid-iteration over a site-supplied list:
        one bad `<loc>` would abort `parse_sitemap` and lose every LATER entry, which is a
        site handing us a malformed URL and getting the rest of its sitemap dropped. A gate
        documented as failing closed has to actually fail closed, so a parse failure is
        False, not an exception."""
        try:
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if scheme not in self._FETCHABLE_SCHEMES:
            return False
        return self._host_is_ours(host)

    def is_same_site(self, url: str) -> bool:
        """Whether a URL belongs to the crawled site, IGNORING whether we would fetch it.

        Split out from `is_internal` in the #89 review, because that predicate now answers a
        different question than it used to. `is_internal` is a FETCH gate: it refuses
        non-http(s) because we must never hand `file://` to a download handler. But
        `content_extractor` uses the same callable to CLASSIFY links, where the question is
        "does this point at the client's own site" -- and there the scheme is irrelevant.

        Passing the fetch gate there had a client-visible consequence:
        `<a href="ftp://<our-domain>/downloads">`, not rare on older association sites,
        stopped counting as an internal link and fell through to the external branch, so the
        report listed the client's OWN domain back to them as a third-party integration.
        Two questions, two predicates."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return self._host_is_ours(host)

    def _host_is_ours(self, host: str) -> bool:
        """The host comparison both predicates share, so they cannot drift apart."""
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
        # `s3:` has a registered Scrapy download handler too, and its absence here is what
        # made the "<a href> was already safe" claim above false (#89 review).
        "s3:",
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
        # Record the actual seconds, not just that it happened (issue #74). "We paced this
        # site at 10s/request because it asked for 15s" is the sentence an operator needs to
        # read a partial crawl correctly; a bare counter can't say it. set_value, not inc:
        # these are a measurement, and robots.txt is parsed once per session.
        stats = getattr(getattr(self, "crawler", None), "stats", None)
        if stats is not None:
            try:
                stats.set_value("robots_crawl_delay_honored", float(honored))
                stats.set_value("robots_crawl_delay_requested", float(requested))
            except Exception:
                self.logger.debug("could not record crawl-delay values", exc_info=True)
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
        # Published HERE rather than in `start()`, because `start_requests()` (the legacy
        # entry point) also funnels through this and was silently skipping it -- invisible to
        # all 764 tests (#99 review). One funnel, so a future third entry point cannot
        # reintroduce the gap.
        self._publish_knob_stats()
        self._stat("seeding/seeds_emitted")
        yield scrapy.Request(
            urljoin(self.start_urls[0], "/robots.txt"),
            callback=self.parse_robots,
            errback=self.robots_failed,
            # dont_filter is LOAD-BEARING for a JOBDIR resume: the dupefilter persists across
            # sessions, so without it session 2 would filter this as already-seen, never run
            # parse_robots, and therefore never emit the start URLs -- a zero-page resume.
            dont_filter=True,
            meta=self._robots_budget_meta(),
        )

    # How long we will wait for robots.txt, PER ATTEMPT, on either download path.
    #
    # Since #76 robots.txt is the crawl's only seed, so in the first session nothing else is
    # in flight while it retries. A black-holed host (accepts the connection, never answers)
    # therefore stalls the WHOLE crawl, where before #76 the start URL was being fetched in
    # parallel throughout and this cost nothing (issue #82). At Scrapy's DOWNLOAD_TIMEOUT of
    # 180s and RETRY_TIMES of 2 that is 3 x 180s = 540s of a 7200s session -- 7.5% of the
    # budget with zero pages fetched. 60s x 3 = 180s instead.
    #
    # WORSE ON THE CRAWLS THIS TOOL EXISTS FOR, which is why the issue's "paid every session"
    # framing is right and my first reading of it was wrong. `--profile presale` -- the
    # politer bundle for prospect sites we do not control -- forces `delay >= 3`, and
    # `run_spider.py` sets `CONCURRENT_REQUESTS: 1` for any delay that high. With ONE slot
    # there is no "other work in flight" to absorb the stall even on a RESUMED session with a
    # full frontier, so a black-holed host costs the full budget every session, not just the
    # first. And the chain multiplies it: bounded, a host that 301s fast and then black-holes
    # still costs 4 hops x 3 attempts x 60s = 720s (10% of a session), versus 2160s unbounded.
    #
    # WHY NOT SHORTER, AND WHY RETRIES ARE UNTOUCHED. The two failure directions here are
    # not symmetric. Waiting too long costs crawl budget; giving up too early routes us to
    # `robots_failed`, which proceeds ALLOW-ALL -- so an aggressive timeout silently converts
    # "this site was slow" into "this site has no robots.txt" and crawls a site that said
    # Disallow. That is #76's harm arriving through the back door, so the bound is sized to
    # be unreachable by any site we could actually crawl: 180s is sized for a page, this is
    # 1KB of text, and a host that cannot deliver it in 60s cannot serve a crawl either.
    DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT = _ROBOTS_TIMEOUT_FLOOR

    # Attempts for robots.txt specifically, pinned rather than inherited (#82 review).
    #
    # Retries are what stop ONE transient blip from flipping the gate open, so the margin
    # matters more here than anywhere else in the crawl -- and inheriting it made the margin
    # depend on a CLI flag: `run_spider.py` sets `RETRY_TIMES: 1` whenever `--impersonate` is
    # on, so the gate got 2 attempts there and 3 on the default path. Same argument as the
    # timeout: whether we honour a site's Disallow must not depend on which flag the operator
    # passed. RetryMiddleware reads this meta key in preference to the setting, so pages keep
    # the operator's RETRY_TIMES while robots.txt keeps its margin on both paths.
    ROBOTS_MAX_RETRY_TIMES = 2

    def _resolve_robots_timeout(self) -> int:
        """The robots.txt per-attempt budget, from `YOKO_CRAWL_ROBOTS_TIMEOUT` (issue #92).

        Unset, unparseable, non-finite, or BELOW the floor -> the floor. Above the ceiling ->
        the ceiling. Both bounds are module constants, NOT `self.` lookups, because Scrapy
        assigns `-a` spider arguments straight into `self.__dict__` -- reading the floor off
        the instance let `-a DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT=1` shadow it and honour a 5s
        budget (#92 review). The floor is not defensive tidiness: this is the one constant
        whose wrong value routes a slow site to `robots_failed` and crawls it allow-all.

        Records the disposition on the INSTANCE, not as a stat, and that is not a style
        choice: `Spider.from_crawler` calls `cls(*args, **kwargs)` and only then
        `_set_crawler`, so during `__init__` there is no `self.crawler` and `_stat` drops
        the write in silence (verified). `start()` republishes these as stats once the
        crawler exists -- see `_publish_knob_stats` (issue #99)."""
        raw = os.environ.get("YOKO_CRAWL_ROBOTS_TIMEOUT")
        self._robots_timeout_requested = raw
        if raw is None:
            self._robots_timeout_disposition = "default"
            return _ROBOTS_TIMEOUT_FLOOR
        try:
            requested = int(float(raw))
        except (TypeError, ValueError, OverflowError) as exc:
            # OverflowError is the load-bearing member of that tuple and NOT a subclass of the
            # other two: `float()` happily accepts "inf"/"Infinity"/"1e400", and `int()` then
            # raises OverflowError on the result. Letting it escape killed spider construction,
            # which `run_spider.py` exits 0 from and the job manager reports as a COMPLETED
            # zero-page crawl (#92 review; the exit-code seam is filed as #98). "nan" is the one
            # non-finite spelling that raises ValueError instead -- which is exactly why having
            # only "nan" in the junk test gave false confidence this class was covered.
            self.logger.warning(
                "YOKO_CRAWL_ROBOTS_TIMEOUT=%r is not a usable number (%s) -- using the %ds "
                "default.", raw, type(exc).__name__, _ROBOTS_TIMEOUT_FLOOR,
            )
            self._robots_timeout_disposition = "invalid"
            return _ROBOTS_TIMEOUT_FLOOR
        if requested < _ROBOTS_TIMEOUT_FLOOR:
            self.logger.warning(
                "YOKO_CRAWL_ROBOTS_TIMEOUT=%r (%ds) is below the %ds floor and was IGNORED. "
                "A shorter robots.txt budget does not speed a crawl up -- it makes a slow "
                "site look like one with no robots.txt, and the crawl then proceeds "
                "allow-all against a site that may have said Disallow. Raise it, not lower "
                "it. (issue #92)",
                raw, requested, _ROBOTS_TIMEOUT_FLOOR,
            )
            self._robots_timeout_disposition = "floored"
            return _ROBOTS_TIMEOUT_FLOOR
        if requested > _ROBOTS_TIMEOUT_CEILING:
            self.logger.warning(
                "YOKO_CRAWL_ROBOTS_TIMEOUT=%r (%ds) exceeds the %ds ceiling and was CLAMPED. "
                "robots.txt is the crawl's only seed, so the stall before the first page is "
                "%d attempts x the budget, multiplied again by redirect hops and sitemap "
                "probes; past the ceiling that outruns the %ds watchdog, which SIGKILLs the "
                "crawl into `failed` with no failure_reason -- worse than the allow-all this "
                "knob guards against. (issue #92)",
                raw, requested, _ROBOTS_TIMEOUT_CEILING,
                self.ROBOTS_MAX_RETRY_TIMES + 1, _JOB_WATCHDOG_TIMEOUT,
            )
            self._robots_timeout_disposition = "clamped"
            return _ROBOTS_TIMEOUT_CEILING
        if requested > _ROBOTS_TIMEOUT_FLOOR:
            self.logger.info(
                "robots.txt fetch budget raised to %ds by YOKO_CRAWL_ROBOTS_TIMEOUT "
                "(default %ds, ceiling %ds).",
                requested, _ROBOTS_TIMEOUT_FLOOR, _ROBOTS_TIMEOUT_CEILING,
            )
        # "honoured", not "default": the operator DID set this, and it was accepted as-is.
        # Reporting `default` for an explicit in-range value tells them nothing about whether
        # it took effect, which is the single question this field exists to answer (#99 review).
        self._robots_timeout_disposition = "raised" if requested > _ROBOTS_TIMEOUT_FLOOR \
            else "honoured"
        return requested

    def _robots_budget_meta(self):
        """Bound the robots.txt fetch on BOTH download paths -- they honour different keys.

        `download_timeout` is set by DownloadTimeoutMiddleware with `setdefault`, so an
        explicit value here wins on the default path. scrapy-impersonate reads neither that
        key nor DOWNLOAD_TIMEOUT itself, which is why this also writes `impersonate_args`.

        SINCE #88, `ImpersonateMiddleware` forwards `download_timeout` into
        `impersonate_args["timeout"]`, so the explicit key here is no longer the ONLY thing
        reaching curl on that path -- but do not delete it as redundant. It is what keeps the
        robots gate's bound INDEPENDENT of the page bound: the forwarder uses `setdefault`,
        so this explicit value still wins, and the two being equal today (both 60s) is a
        coincidence of the numbers rather than a property. Move DOWNLOAD_TIMEOUT and the
        robots budget stays where it was put, which is the point.

        Setting both keys deliberately makes the impersonate path wait LONGER than it does
        today, 2 x 60s rather than 2 x 30s. Robots semantics must not depend on which
        `--impersonate` flag the operator passed, and 30s is inside the range a slow-but-real
        site can take -- on that path a slow robots.txt is silently abandoned and the crawl
        proceeds allow-all. Impersonation is used for exactly the WAF-fronted sites where
        that matters most. The extra 60s worst case is a fifth of what #82 gives back.

        Note the two paths' arithmetic differs and neither number is "the" worst case:
        default path 3 x 60s = 180s (down from 540s), impersonated 3 x 60s = 180s (up from
        2 x 30s = 60s, since `--impersonate` also sets `RETRY_TIMES: 1` -- see
        ROBOTS_MAX_RETRY_TIMES, which is why the attempt count is pinned here rather than
        inherited)."""
        return {
            "download_timeout": self.robots_download_timeout,
            "impersonate_args": {"timeout": self.robots_download_timeout},
            "max_retry_times": self.ROBOTS_MAX_RETRY_TIMES,
        }

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
        # Record the probe's answer as a STAT, not just a log line (#81 review). This is the
        # failure class `docs/solutions/conventions/silent-orphaning-framework-extension-points.md`
        # was written about, and it prescribes counting the thing that should have happened
        # and surfacing it -- which `stats_extension` already does for the sibling seeding
        # tripwire two functions away. A warning buried in the Scrapy log of a hand-managed
        # droplet is not that. Always set, so `0` is a positive assertion the key works
        # rather than an ambiguous absence.
        self._stat("seeding/depth_reset_unsupported", 0 if _DEPTH_RESET_SUPPORTED else 1)
        if not _DEPTH_RESET_SUPPORTED:
            # requirements.txt now floors Scrapy at 2.18 for this, but a floor only binds a
            # fresh install and this crawler is deployed by hand onto a long-lived droplet
            # venv. On an older Scrapy `depth_reset` is simply an unrecognised meta key --
            # ignored in silence, leaving the #81 off-by-one in place while the code reads
            # as if it were fixed. Say so out loud instead.
            self.logger.error(
                "Scrapy %s does not support the `depth_reset` meta key (added in 2.18), so "
                "the start URL stays at depth 1 and every page below it is off by one. "
                "Harmless while DEPTH_LIMIT is unset; upgrade before relying on a depth "
                "bound. (issue #81)", scrapy.__version__,
            )
        for url in self.start_urls:
            self._stat("seeding/seeds_emitted")
            # `start_urls_emitted` is the SECOND half of the seeding tripwire (#52/#76).
            # Seeding used to be one atomic event, so `seeds_emitted == 0` was a sound test
            # of "did seeding run". It is now a two-phase protocol -- seed robots.txt, then
            # emit the start URLs from a callback -- and a failure to reach phase two is the
            # zero-page crawl this whole change has to avoid. Counted separately so
            # stats_extension can assert BOTH halves ran.
            self._stat("seeding/start_urls_emitted")
            yield scrapy.Request(
                url, callback=self.parse, errback=self.page_failed, dont_filter=True,
                # Put the start URL back at depth 0 (issue #81). It is yielded from a
                # callback now, so DepthMiddleware reads it as a CHILD of the robots.txt
                # response and stamps depth=1 -- two when robots.txt redirected -- shifting
                # every page below it by the same amount. DEPTH_LIMIT is unset today so
                # nothing breaks, but #54 contemplates bounding depth and whoever lands it
                # would silently get N-1 levels for a requested N, with the cause sitting in
                # a change that predates their work.
                #
                # It ALSO re-separates BFO ordering -- homepage links go back to depth 1 and
                # again outrank sitemap-listed URLs at depth 2 instead of tying. That is a
                # TRADE, not a free win, and the earlier version of this comment overclaimed
                # it. Depth is the BFO tiebreak (DEPTH_PRIORITY=1), and Scrapy's priority
                # queue drains EVERY depth-1 request before ANY depth-2 one -- so on a site
                # whose homepage links straight into a shallow-wide facet trap, that trap now
                # claims the whole budget ahead of sitemap-curated content, where today's
                # off-by-one tie interleaves them.
                # `docs/solutions/architecture-patterns/queue-discipline-turns-a-url-trap-into-a-trapdoor.md`
                # measured exactly this (289 of 300 pages lost to a shallow-wide trap that
                # BFO does not bound) and names #54's DEPTH_LIMIT / per-prefix cap as the
                # real fix. Restoring the pre-#76 ordering is still right -- the alternative
                # is keeping an accidental mitigation produced by a bug -- but #54 is what
                # actually bounds the trap.
                #
                # ON THE `robots_failed` PATH THIS KEY DOES NOTHING. Scrapy runs no spider
                # middleware on errback output, so DepthMiddleware never sees these requests
                # and never consumes the key. Depth still comes out right there, via
                # `_init_depth`'s base case (no `depth` in meta -> the response is depth 0),
                # so the behaviour is correct on both paths but by two different mechanisms.
                # Harmless to send, and sending it unconditionally keeps one code path.
                #
                # Note `meta={"depth": 0}` does NOT work -- the issue's suggested shape.
                # DepthMiddleware.get_processed_request ASSIGNS `request.meta["depth"]`
                # unconditionally, so a pre-set value is overwritten and the fix would look
                # applied while changing nothing. `depth_reset` is the supported key.
                meta={"depth_reset": True},
                # Suppress the Referer. The start URL is now yielded from a callback, so
                # Scrapy's RefererMiddleware would stamp `/robots.txt` on it and _emit_row's
                # header fallback would report the site root as "linked from robots.txt" --
                # a value yoko-corpus persists into page_versions.referrer and exports. The
                # site root has no referrer; keep the emitted row byte-identical to before.
                headers={"Referer": None},
            )

    def robots_failed(self, failure):
        """robots.txt never arrived -- DNS failure, refused connection, timeout (issue #76).

        Scrapy routes transport failures to an errback, not the callback, so without this
        the start URLs would never be emitted and a momentary network fault would silently
        produce a ZERO-page crawl. Allow-all is the same posture as a site with no
        robots.txt: we could not read a "no", so we do not invent one."""
        self._stat("seeding/robots_failed")
        # ONE signal regardless of route (#97): `robots_failed` covers transport failures
        # only, so a 403/503 -- the cheaper and more common way to end up without rules --
        # left no trace at all. Both routes now record the same readability outcome.
        self._record_robots_readability(None)
        # Log the exception TYPE, not just its str() (#82 review). Scrapy 2.18 wraps every
        # download exception in its own class, and those wrappers carry NO message:
        # str(DownloadTimeoutError()), str(CannotResolveHostError()) and
        # str(DownloadConnectionRefusedError()) are all `''`. So this line -- the ONLY signal
        # that a crawl proceeded allow-all -- was rendering as "robots.txt could not be
        # fetched ()" for essentially every real failure on the default path. It went
        # unnoticed because the test for it passes an OSError, which stringifies fine: the
        # fixture did not match the shape production produces. #82's shorter bound makes the
        # timeout variant routine, so the blank got worse.
        exc = failure.value if hasattr(failure, "value") else failure
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        self.logger.warning(
            "robots.txt could not be fetched (%s) -- proceeding allow-all, as for a site "
            "with no robots.txt. Crawl-delay and Disallow rules are unknown for this crawl.",
            detail,
        )
        # It named no sitemap, because we never read it (#77).
        try:
            yield from self._sitemap_probe_requests()
        except Exception:
            self.logger.debug("sitemap probing failed", exc_info=True)
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

    def _publish_knob_stats(self):
        """Republish the operator knobs' resolved values as stats (issue #99).

        `__init__` resolves them, but Scrapy attaches `self.crawler` only AFTER `__init__`
        returns, so a stat written there is dropped in silence. `start()` is the first point
        that definitely has a crawler and runs exactly once per session, so it is where the
        values become observable.

        Until now the record of which budget was in force was a log line, in the Scrapy log
        of a hand-managed droplet -- for the exact scenario the knob exists to serve, a slow
        crawl an operator is debugging. For the delay cap's invalid case there was no record
        at ALL: it falls back silently, so an unparseable value left no trace anywhere. The sibling crawl-delay knob has surfaced its
        applied/honored/requested values in `restrictions` since #74; this brings the pair
        into line rather than inventing a convention."""
        stats = getattr(getattr(self, "crawler", None), "stats", None)
        if stats is None:
            return
        # Wrapped for the same reason `_record_robots_readability` is: this now runs at the
        # head of the crawl's ONLY seeding path, and observability must never be the thing
        # that costs a crawl its seeds.
        try:
            self._set_knob_stats(stats)
        except Exception:
            self.logger.debug("publishing knob stats failed", exc_info=True)

    def _set_knob_stats(self, stats):
        stats.set_value("robots_timeout_effective", self.robots_download_timeout)
        stats.set_value("robots_timeout_requested", self._robots_timeout_requested)
        stats.set_value("robots_timeout_disposition", self._robots_timeout_disposition)
        stats.set_value("robots_max_delay_effective", self.max_robots_crawl_delay)
        stats.set_value("robots_max_delay_requested", self._max_delay_requested)
        stats.set_value("robots_max_delay_disposition", self._max_delay_disposition)

    def start_requests(self):
        """Seed the crawl on Scrapy < 2.13, where `start()` does not exist.

        UNREACHABLE under the current floor: #81 raised `requirements.txt` to
        `scrapy>=2.18,<3`, so no supported version lacks `start()`. Kept anyway, because it
        costs one line and the failure it guards is the one this whole file is scarred by
        (#52): a version outside the declared range is exactly the situation where seeding
        goes missing in SILENCE, and a droplet venv is installed by hand. Deleting it would
        trade a free fallback for a zero-page crawl on the one machine that matters."""
        return self._seed_requests()

    # ---------- Robots & sitemaps ----------

    # How many on-domain redirects of /robots.txt we will follow before giving up and
    # seeding anyway. robots.txt legitimately redirects once or twice (http->https,
    # apex->www); beyond that it is a loop or a misconfiguration. Bounded explicitly
    # because seeding now hangs off this callback (issue #76) -- an unbounded chain would
    # spin until CLOSESPIDER_TIMEOUT and never emit a single page.
    MAX_ROBOTS_REDIRECTS = 3

    def parse_robots(self, response):
        """Handle the robots.txt response and then seed the crawl.

        Seeding hangs off this callback (issue #76), which makes every failure in here a
        potential ZERO-PAGE crawl -- strictly worse than the race it replaced. So the work
        is done in `_robots_outputs`, which is written never to raise, and the start URLs
        are emitted here on every path except a redirect we are still following (where the
        next hop seeds instead)."""
        self._stat("seeding/robots_fetched")
        outputs, seeding_deferred = self._robots_outputs(response)
        yield from outputs
        if not seeding_deferred:
            yield from self._start_url_requests()

    def _robots_outputs(self, response):
        """Everything parse_robots wants to emit, plus whether seeding is deferred to a
        redirect hop. Returns `(outputs, seeding_deferred)`.

        MUST NOT RAISE. A malformed `Location` header or a malformed `Sitemap:` URL both
        reach `urlparse`, which raises ValueError on a netloc that fails NFKC normalization
        (a fullwidth solidus pasted into a CMS robots.txt does it) or on an unterminated
        `[`. Before seeding moved here that cost only sitemap discovery; now an escaping
        exception would skip the seed entirely and the crawl would report `completed` with
        one row. Each stage is therefore contained separately, so a failure in one still
        leaves the others -- and the seed -- intact."""
        outputs = []

        try:
            outputs.extend(self._emit_row(response))
        except Exception:
            self.logger.warning(
                "Could not emit the robots.txt row for %s -- continuing so the crawl "
                "still seeds.", response.url, exc_info=True,
            )

        # One-hop redirect follow -- only on-domain (issue corpus#71). robots.txt should redirect
        # within the site (http->https, apex->www); an off-domain hop is a handoff to another site,
        # not our robots, so don't fetch it as ours.
        if response.status in self.REDIRECT_STATUSES:
            nxt = self._robots_redirect_request(response)
            if nxt is not None:
                outputs.append(nxt)
                return outputs, True  # the followed hop seeds instead
            # No Location, an off-domain target, a malformed one, or the hop cap: the robots
            # chain ends here, so this call has to seed or the crawl fetches nothing. A
            # robots.txt we could not READ named no sitemap by definition, so probe (#77).
            #
            # This branch RETURNS before `_robots_body_outputs`, so it needs its own
            # readability record or a dead-ended redirect -- a real "we never read the
            # rules" outcome -- would be filed as `unknown` rather than `unreadable` (#97).
            self._record_robots_readability(response)
            try:
                outputs.extend(self._sitemap_probe_requests())
            except Exception:
                self.logger.debug("sitemap probing failed", exc_info=True)
            return outputs, False
        return self._robots_body_outputs(response, outputs), False

    def _robots_redirect_request(self, response):
        """The next on-domain robots.txt request for a redirect, or None when the chain
        ends here (no Location, off-domain, malformed, or the hop cap reached).

        The hop cap is load-bearing: `REDIRECT_ENABLED` is False, so Scrapy's own
        REDIRECT_MAX_TIMES never applies, and the followed request carries dont_filter
        (it must, or a retried chain would be dropped with no errback and no seed). An
        apex/www pair whose robots.txt rules point at each other would otherwise loop for
        the full CLOSESPIDER_TIMEOUT, emit two rows, and report `completed`."""
        hops = 0
        try:
            hops = int(response.request.meta.get("robots_hops", 0))
        except Exception:
            hops = 0
        if hops >= self.MAX_ROBOTS_REDIRECTS:
            self._stat("seeding/robots_redirect_limit")
            self.logger.warning(
                "robots.txt redirected more than %d times (last: %s) -- giving up on it and "
                "crawling allow-all, as for a site with no robots.txt.",
                self.MAX_ROBOTS_REDIRECTS, response.url,
            )
            return None
        target = response.headers.get("Location")
        if not target:
            return None
        try:
            tgt = response.urljoin(target.decode("latin-1"))
            if not self.is_internal(tgt):
                return None
        except Exception:
            self.logger.warning(
                "Unparseable Location on %s -- treating robots.txt as unreadable and "
                "crawling allow-all.", response.url, exc_info=True,
            )
            return None
        # Same per-attempt bound as the first fetch (#82). Every hop is still the crawl's
        # only request, so an unbounded hop is an unbounded stall -- and the chain multiplies
        # it: MAX_ROBOTS_REDIRECTS of 3 means up to FOUR black-holed fetches, which at the
        # un-bounded 540s each is 2160s, 30% of a 7200s session, before a page is fetched.
        #
        # `depth_reset` keeps every hop at depth 0, like the first fetch (#81 review). Without
        # it each hop is a child of the last, and the moment #54 sets DEPTH_LIMIT,
        # DepthMiddleware returns None for a hop past the bound -- silently dropping the
        # request that SEEDING WAS DEFERRED TO. Reproduced on a real engine: a robots.txt
        # that redirects twice with DEPTH_LIMIT=1 yields 2 rows, both robots.txt itself,
        # `start_urls_emitted` unset, closing `finished`. That is the zero-page crawl #76
        # exists to prevent, arriving through the fix for it. Sitemap probes stay at depth 1
        # and sitemap URLs at depth 2, so this changes no page ordering.
        return scrapy.Request(
            tgt, callback=self.parse_robots, errback=self.robots_failed,
            dont_filter=True,
            meta={
                "robots_hops": hops + 1,
                "depth_reset": True,
                **self._robots_budget_meta(),
            },
        )

    # robots.txt statuses that genuinely mean "this site has no rules" (issue #97).
    #
    # The only response class from which allow-all is an inference we are entitled to make.
    # Everything else -- a 403, a 429, a 5xx, a 200 we could not decode -- means we did not
    # READ the rules, a different fact that must not be recorded as the same one.
    #
    # NOTE this is STRICTER than RFC 9309, and an earlier version of this comment wrongly
    # claimed the RFC's backing for it. The RFC treats most 4xx as "unavailable" and lets a
    # crawler assume no restrictions on a 403 (it treats 5xx the other way). We put 403 in
    # `unreadable` because in OUR population a 403 on robots.txt is usually an edge WAF
    # refusing us rather than an origin saying it has no rules -- that is our reasoning and
    # it should be defended on its own terms, not by borrowing the RFC's authority.
    ROBOTS_ABSENT_STATUSES = frozenset({404, 410})

    def _record_robots_readability(self, response=None, parsed_now=False):
        """Record WHY this crawl does or does not hold robots.txt rules (issue #97).

        Until now the only signal that a crawl proceeded without rules was
        `seeding/robots_failed`, which fires on TRANSPORT failure alone. A site that answers
        with a 403 or a 503 produces a perfectly ordinary response, so the body is discarded
        by the `status == 200` gate above, the rules stay unset (allow-all) and NOTHING is
        counted -- the tripwire is silently incomplete on the cheaper route. Cloudflare 403s
        a robots.txt routinely, so this is not a corner case.

        `outcome` describes THIS SESSION'S FETCH, not the crawl's accumulated knowledge, and
        that distinction is load-bearing rather than pedantic. `self._robots` is restored
        from `self.state` on a resumed session BEFORE this runs, so classifying on "do we
        hold rules" reported `parsed` for a session that was refused -- meaning a site that
        blocked us for thirty-nine consecutive sessions after one good one left NO trace.
        Under-counting refusals is the one direction that actively argues for the wrong
        policy in #97, so the refusal is recorded and `rules_from_state` carries the other
        fact alongside it. Both survive; neither overwrites the other.

        Deliberately an observation, not a verdict -- it changes no crawl behaviour. Whether
        `unreadable` should stop a crawl instead of proceeding allow-all is the posture
        question in #97, and it is Sarah's to answer with these counts in hand.

        MUST NOT RAISE: `_robots_outputs` contains every other stage separately because an
        escape here skips the seed and yields a one-row crawl reporting `completed`. This is
        a measurement; it must never be the thing that breaks the crawl it measures."""
        try:
            if response is None:                  # transport failure -- robots_failed
                outcome, status, edge_wall = "unreadable", None, None
            elif parsed_now:                      # Protego parsed THIS session's body
                outcome, status, edge_wall = "parsed", int(response.status), None
            elif int(response.status) in self.ROBOTS_ABSENT_STATUSES:
                outcome, status, edge_wall = "absent", int(response.status), None
            else:
                # Non-200, an undecodable 200, or a body Protego refused.
                #
                # WHICH edge refused us, or None for an origin refusal (#100). #97 shipped
                # this as a Cloudflare-only boolean and said so honestly -- a FLOOR on edge
                # refusals rather than the whole of them -- because `_is_waf_challenge` keys
                # on cf-mitigated / cf-ray + Server and files every Sucuri, Akamai, Imperva
                # and AWS wall as an ORIGIN refusal. That is the exact split #97's posture
                # question turns on, biased in the same direction as the two under-counts
                # review already found there.
                #
                # `_is_waf_challenge` itself is untouched: it drives `waf_challenge_count`,
                # which yoko-corpus uses to trigger an impersonation/proxy retry, so widening
                # it changes client crawl BEHAVIOUR rather than a measurement. Filed separately.
                outcome, status = "unreadable", int(response.status)
                edge_wall = self._edge_wall_vendor(response)
            rules_from_state = self._robots is not None and not parsed_now

            self._stat(f"seeding/robots_{outcome}")
            if outcome == "unreadable":
                self._stat(f"seeding/robots_unreadable_{edge_wall}" if edge_wall
                           else "seeding/robots_unreadable_origin")
                self.logger.warning(
                    "robots.txt was NOT READ (HTTP %s%s) -- proceeding allow-all%s. This is "
                    "not the same as a site with no robots.txt: rules may exist and we could "
                    "not see them. (issue #97)",
                    status if status is not None else "-",
                    f", {edge_wall} wall" if edge_wall else "",
                    " for anything not covered by rules carried over from an earlier session"
                    if rules_from_state else
                    "; Crawl-delay and Disallow are unknown for this crawl",
                )
            crawler = getattr(self, "crawler", None)
            stats = getattr(crawler, "stats", None) if crawler else None
            if stats is not None:
                stats.set_value("robots_readability_outcome", outcome)
                stats.set_value("robots_readability_status", status)
                stats.set_value("robots_readability_edge_wall", edge_wall)
                stats.set_value("robots_readability_rules_from_state", rules_from_state)
        except Exception:
            self.logger.debug("recording robots.txt readability failed", exc_info=True)

    def _robots_body_outputs(self, response, outputs):
        """Parse the rules and discover sitemaps. Best-effort in both halves."""
        # Parse robots.txt rules from the real body (issues #57/#59): Disallow gates scheduling
        # (is_robots_disallowed) and a Crawl-delay paces the host. Best-effort -- a malformed or
        # non-text robots.txt leaves rules unset (allow-all), never breaking the crawl.
        parsed_now = False
        if response.status == 200 and isinstance(response, TextResponse):
            try:
                body = response.text
                self._robots = Protego.parse(body)
                # Set the instant the rules exist, BEFORE the best-effort work below: a
                # failure to persist or to apply a Crawl-delay does not un-parse them.
                parsed_now = True
                self._persist_robots_body(body)
                self._record_robots_scope()
                delay = self._robots.crawl_delay(self.ROBOTS_USER_AGENT)
                if delay:
                    self._apply_crawl_delay(float(delay))
            except Exception:
                self.logger.debug("robots.txt parse failed for %s", response.url, exc_info=True)
        # AFTER the parse attempt, and told explicitly whether it succeeded -- `parsed` must
        # mean rules THIS session read, not merely a 200 (a malformed body reaches here and
        # leaves us with nothing) and not rules a resume restored (#97 review).
        self._record_robots_readability(response, parsed_now=parsed_now)

        # Discover sitemaps -- only on-domain (a robots.txt can list a third-party sitemap URL).
        # Guarded on TextResponse: a binary body has no `.text`. Each line is guarded on its
        # own so ONE malformed `Sitemap:` URL cannot cost the others, or the seed.
        listed_a_sitemap = False
        if isinstance(response, TextResponse):
            try:
                lines = response.text.splitlines()
            except Exception:
                lines = []
            for line in lines:
                if not line.lower().startswith("sitemap:"):
                    continue
                try:
                    sm_url = line.split(":", 1)[1].strip()
                    # urljoin FIRST (#89 review). robots.txt is supposed to carry an absolute
                    # Sitemap: URL and plenty of sites do not, so a bare `/sitemap.xml` used
                    # to fail the host check and a `//host/sitemap.xml` used to pass it and
                    # then raise in scrapy.Request. Resolving against the robots.txt response
                    # recovers both; an absolute hostile scheme passes through urljoin
                    # unchanged and is still refused below.
                    sm_url = self._resolve_site_url(response, sm_url)
                    if sm_url and self.is_internal(sm_url):
                        outputs.append(scrapy.Request(
                            sm_url, callback=self.parse_sitemap,
                            errback=self.sitemap_failed, dont_filter=True,
                        ))
                        # Set AFTER the Request exists: the flag suppresses the #77 probe
                        # fallback, so setting it first meant a Sitemap: line we failed to
                        # queue still cost us the fallback that exists to cover that case.
                        listed_a_sitemap = True
                except Exception:
                    self._stat("seeding/sitemap_url_unparseable")
                    self.logger.warning(
                        "Skipping an unparseable Sitemap: line in %s", response.url,
                        exc_info=True,
                    )
        # Sitemap discovery was single-source (issue #77): a site whose robots.txt omits the
        # `Sitemap:` line got NO sitemap seeding and silently degraded to link-following
        # only -- the same class of silent degradation as #52, reached a different way.
        if not listed_a_sitemap:
            # Contained like every other stage here: _robots_outputs must not raise, or the
            # seed is skipped and the crawl reports `completed` with nothing (issue #76).
            try:
                outputs.extend(self._sitemap_probe_requests())
            except Exception:
                self.logger.debug("sitemap probing failed", exc_info=True)
        return outputs

    # Conventional sitemap locations, probed only when robots.txt names none (issue #77).
    # Ordered most-likely-first so the common case is found on the first hit; every one is
    # tried regardless (a site can serve several), but a hit on any is enough to recover the
    # coverage. `/sitemap_index.xml` leads because Yoast is the most common WP setup and is
    # exactly what gastro.org serves -- 2,347 URLs we never looked for, because its
    # robots.txt has no `Sitemap:` line.
    SITEMAP_PROBE_PATHS = (
        "/sitemap_index.xml",   # Yoast
        "/sitemap.xml",         # the de facto standard
        "/wp-sitemap.xml",      # WordPress core >= 5.5
        "/sitemap-index.xml",
    )

    def _sitemap_probe_requests(self):
        """Speculative fetches of the conventional sitemap locations.

        Only reached when robots.txt listed no on-domain `Sitemap:`, so this costs a handful
        of requests on the miss path and nothing at all on a site that points us at its own.
        Robots-disallowed paths are skipped -- a probe is still a fetch, and guessing a URL
        is not a reason to stop obeying the site."""
        for path in self.SITEMAP_PROBE_PATHS:
            url = urljoin(self.start_urls[0], path)
            if self.is_robots_disallowed(url):
                self._stat("seeding/sitemap_probes_disallowed")
                continue
            self._stat("seeding/sitemap_probes_sent")
            yield scrapy.Request(
                url, callback=self.parse_sitemap_probe, errback=self.sitemap_probe_failed,
                dont_filter=True,
                # Suppress the Referer for the same reason the start URL does: these are
                # yielded from parse_robots, so RefererMiddleware would stamp robots.txt on
                # them and the emitted row would assert that robots.txt LINKED to this
                # sitemap -- false in the only situation this code can run, since robots.txt
                # naming no sitemap is the trigger.
                headers={"Referer": None},
                # Same per-attempt bound as robots.txt (#82 review). These are on the SAME
                # seeding path and are emitted before any page exists, so an unbounded probe
                # is the same stall: under `--profile presale` (CONCURRENT_REQUESTS 1) the
                # four of them run one at a time, up to 4 x 540s = 2160s -- 30% of a session
                # -- against a host that black-holes the probe paths specifically. They are
                # also 1KB-of-XML fetches, so the same sizing argument applies unchanged.
                #
                # `depth` is set EXPLICITLY rather than left implicit because the two seeding
                # paths disagree otherwise: DepthMiddleware overwrites it to 1 on the
                # `parse_robots` path (no change), but runs at all on the `robots_failed`
                # path, where errback output skips the spider middleware entirely and
                # `_init_depth` would otherwise call these depth 0 -- so the same site got
                # different probe depths depending on whether robots.txt answered.
                meta={
                    "guessed_source": True, "probe_hops": 0, "depth": 1,
                    **self._robots_budget_meta(),
                },
            )

    def parse_sitemap_probe(self, response):
        """A GUESSED sitemap URL coming back. Unlike `parse_sitemap` this emits NOTHING
        unless the guess was right.

        That distinction is the whole point. `parse_sitemap` emits a row for every response
        it sees, which is correct for a URL the site told us about -- but these are URLs we
        invented, and most sites will 404 three of the four. Emitting those would put
        phantom broken links in the crawl, and the report presents 404s as "broken links on
        the site" with a referrer. We would be inventing defects in a client's site and then
        reporting them back."""
        # A redirect is the STRONGEST evidence a sitemap exists -- a site that 301s
        # /sitemap.xml somewhere is telling us where it lives. `parse_sitemap` already
        # follows one on-domain hop; dropping it here lost the discovery entirely for any
        # site that redirects to a path we don't guess (/sitemaps/sitemap.xml, an apex->www
        # hop). Bounded to one hop, on-domain only, same as the sibling.
        if response.status in self.REDIRECT_STATUSES:
            target = response.headers.get("Location")
            hops = response.meta.get("probe_hops", 0)
            if target and hops < 1:
                try:
                    tgt = response.urljoin(target.decode("latin-1"))
                except Exception:
                    tgt = None
                if tgt and self.is_internal(tgt):
                    yield scrapy.Request(
                        tgt, callback=self.parse_sitemap_probe,
                        errback=self.sitemap_probe_failed, dont_filter=True,
                        headers={"Referer": None},
                        # Carries the budget for the same reason as the first probe (#82
                        # review) -- bounding only the first hop leaves the follow unbounded.
                        meta={
                            "guessed_source": True, "probe_hops": hops + 1,
                            **self._robots_budget_meta(),
                        },
                    )
                    return
            self._stat("seeding/sitemap_probes_missed")
            return
        if response.status != 200 or not isinstance(response, TextResponse):
            self._stat("seeding/sitemap_probes_missed")
            return
        # Slice BEFORE stripping: `.lstrip()` on a 64MB body copies the whole thing for a
        # 512-char peek. Skip an XML declaration, a BOM, a stylesheet PI, comments and
        # whitespace, then require the root element -- `<urlset` appearing anywhere in the
        # first 512 bytes would accept an HTML page that merely mentions it. Prefix-agnostic
        # (`<sm:urlset`), matching the `local-name()` xpath this feeds.
        try:
            head = response.text[:4096]
        except Exception:
            self._stat("seeding/sitemap_probes_missed")
            return
        if not self._looks_like_sitemap(head):
            # A soft-404 HTML page, or a catch-all route. Common enough that treating it as
            # a sitemap would be worse than missing one.
            self._stat("seeding/sitemap_probes_not_a_sitemap")
            return
        self._stat("seeding/sitemap_probes_found")
        self.logger.info(
            "Found a sitemap at %s -- robots.txt listed none (issue #77)", response.url
        )
        yield from self.parse_sitemap(response)

    # An XML prologue: declaration, BOM, stylesheet/other PIs, comments, DOCTYPE, whitespace.
    _XML_PROLOGUE = re.compile(
        r"^(?:\ufeff|\s+|<\?[^>]*\?>|<!--.*?-->|<!DOCTYPE[^>]*>)+", re.DOTALL | re.IGNORECASE
    )
    # The root element, optionally namespace-prefixed.
    _SITEMAP_ROOT = re.compile(r"^<(?:[A-Za-z0-9_.-]+:)?(?:urlset|sitemapindex)[\s>]", re.IGNORECASE)

    @classmethod
    def _looks_like_sitemap(cls, head: str) -> bool:
        """Whether this body's ROOT element is a sitemap. Anchored, so an HTML page that
        merely contains the string `<urlset` is rejected; prefix-tolerant, so `<sm:urlset`
        is accepted -- the xpath this feeds uses local-name() and would have parsed it."""
        return bool(cls._SITEMAP_ROOT.match(cls._XML_PROLOGUE.sub("", head, count=1).lstrip()))

    def sitemap_failed(self, failure):
        """A sitemap the site ADVERTISED that we could not fetch. Counted, not emitted as a
        row: a sitemap is not a page, so reporting it under "pages we couldn't reach" would
        be wrong. Without an errback it was an unhandled failure in the crawl log."""
        self._stat("sitemap_fetch_failed")
        self.logger.warning("Could not fetch a listed sitemap: %s", failure)

    def sitemap_probe_failed(self, failure):
        """A probe that never landed (DNS/refused/timeout). Speculative, so it is counted
        and dropped -- it must never touch the crawl's outcome."""
        self._stat("seeding/sitemap_probes_missed")
        self.logger.debug("sitemap probe failed: %s", failure)

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
                    yield scrapy.Request(
                        tgt, callback=self.parse_sitemap, errback=self.sitemap_failed,
                    )
            return

        # Skip non-text sitemaps like .gz
        if not isinstance(response, TextResponse):
            self.logger.info("Skipping non-text sitemap: %s", response.url)
            return

        # Pull <loc> values from XML (supports sitemap + index). Provenance rides along:
        # a sitemap we GUESSED the location of is an unverified list (issue #77).
        guessed = bool(response.meta.get("guessed_source"))
        for loc in response.xpath("//*[local-name()='loc']/text()").getall():
            # Resolved against the sitemap, same reason as the Sitemap: line above (#89).
            loc = self._resolve_site_url(response, loc)
            if loc and self.is_internal(loc):
                yield from self._schedule(
                    loc,
                    referrer_emit=self.normalize_url(response.url, exclude_params=self.exclude_params_emit),
                    guessed_source=guessed,
                )

        # Follow nested sitemap indexes if present
        for sm in response.xpath("//*[local-name()='sitemap']/*[local-name()='loc']/text()").getall():
            sm = self._resolve_site_url(response, sm)
            if sm and self.is_internal(sm):
                yield scrapy.Request(
                    sm, callback=self.parse_sitemap, errback=self.sitemap_failed,
                    dont_filter=True,
                    meta={"guessed_source": True} if guessed else {},
                )

    # ---------- Main parse ----------

    def parse(self, response):
        # A dead URL from a sitemap WE GUESSED the location of is not a defect in the
        # client's site (issue #77 review). robots.txt naming no sitemap is the ONLY
        # condition under which we guess, so a guessed file is by construction one the site
        # chose not to advertise -- very often a leftover from a previous platform. A stale
        # /sitemap.xml listing 400 pre-migration URLs would otherwise emit 400 real 404 rows:
        # the corpus counts every 4xx into blocked_page_count, divides by page_count, and
        # flips the report to `wholesale_blocked` at 90% -- telling the client "we couldn't
        # read this site" about a site we read perfectly, and inflating the headline page
        # count with pages that do not exist.
        #
        # So it is emitted as a SKIP row instead: the corpus routes any row with a
        # skip_reason to excluded_urls, never page_versions, so it never counts as a page,
        # never scores as a block, and still surfaces honestly under coverage. A LIVE page
        # from a guessed sitemap is a completely normal row -- this only diverts the dead
        # ones, which is exactly the class we have no business asserting anything about.
        if response.meta.get("guessed_source") and self._is_error_status(response.status):
            self.crawler.stats.inc_value("guessed_sitemap_dead_urls")
            yield self._skip_row(
                self.normalize_url(response.url, exclude_params=self.exclude_params_emit),
                "guessed_sitemap_dead",
                response.meta.get("referrer_emit") or "",
            )
            return

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

        # Platform fingerprint (corpus #112). BELOW the content-type guard on purpose: a
        # linked .json/.csv export is not in ASSET_EXTENSIONS, so it arrives here as a full
        # GET, and running a selector over it would build an lxml tree across up to
        # DOWNLOAD_MAXSIZE (64MB) of non-markup for nothing.
        self._record_platform_signals(response)

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
    _CF_COOKIE_PREFIXES = ("cf_", "__cf")   # kept: `_is_waf_challenge` is Cloudflare-only

    def _has_origin_fingerprint(self, response) -> bool:
        """True when a response carries a header only the origin application sets -- proof
        it was generated by the origin and merely proxied through Cloudflare, not by CF
        itself. Used to keep a proxied origin 403 (member-restricted content) from being
        miscounted as a bot-wall challenge on Cloudflare-fronted sites (where every response
        also carries `cf-ray`/`server: cloudflare`).


        #100 briefly widened the cookie set here so other vendors' cookies would not read as
        origin evidence. That is reverted: `_abck`, `bm_sv` and `awsalb` are passive bot-
        management and stickiness cookies present on ORDINARY responses, so treating them as
        edge evidence removed real origin proof and flipped legitimate origin 403s into
        claimed walls -- the over-attribution this guard exists to prevent."""
        headers = response.headers
        if any(headers.get(h) for h in self._ORIGIN_HEADER_FINGERPRINTS):
            return True
        # A Set-Cookie whose name isn't one of Cloudflare's own cookies is the origin's.
        for raw in headers.getlist("Set-Cookie"):
            name = raw.decode("latin-1").split("=", 1)[0].strip().lower()
            if name and not name.startswith(self._CF_COOKIE_PREFIXES):
                return True
        return False

    # Headers an edge stamps ONLY on a response IT generated as a block (issue #100).
    #
    # This list is deliberately short, and the first cut was not. That version also matched
    # FRONTING headers -- ones present on every response a vendor proxies -- guarded by the
    # origin-fingerprint check. Review showed that does not work, because a fronting header
    # cannot distinguish "this site sits behind X" from "X refused us", and the guard is a
    # heuristic rather than proof. Concretely it produced: any 503 outage behind CloudFront
    # read as an AWS wall (the row required no header at all, only `Server: awselb`); API
    # Gateway's own 403 -- an ORIGIN refusal -- read as a decisive AWS wall; and `x-cdn`, which
    # many CDNs send, filed all of them as Imperva.
    #
    # The bias that created is the dangerous one. This field feeds #97's "should an unreadable
    # robots.txt stop the crawl" and #107's "should non-CF walls trigger a retry", and both
    # over-attribution paths keyed on the huge population of "site behind a big CDN, challenge
    # status" while the under-attribution paths needed a stacked edge. So the bucket read HIGH,
    # arguing FOR stopping crawls and FOR retrying -- while the comment beside it claimed the
    # opposite direction, which is what would have made reading the counts straight off unsafe.
    #
    # So: a vendor is named only on evidence that the EDGE authored the response. Coverage is
    # now partial by construction -- an Imperva or Akamai wall reports `None`, the same as an
    # origin refusal -- and that under-count is the honest failure direction for a field whose
    # whole job is to say "the site refused us". Extending coverage needs REAL captured headers
    # from walled sites rather than more remembered ones (#108).
    _EDGE_GENERATED_SIGNALS = (
        # Sucuri stamps this on its own block page; the name is the claim.
        ("sucuri", ("x-sucuri-block",)),
        # AWS WAF's action header. NOT `x-amzn-errortype`, which API Gateway sets on its own
        # application errors -- that is the origin talking, and it was being read as a wall.
        ("aws", ("x-amzn-waf-action",)),
    )

    def _edge_wall_vendor(self, response):
        """Which edge vendor refused us, or None when we cannot prove one did (issue #100).

        `_is_waf_challenge` answers a narrower question -- "is this a CLOUDFLARE wall" -- and
        drives `waf_challenge_count`, which yoko-corpus uses to decide whether to retry a crawl
        through impersonation or a proxy. It is deliberately left alone: widening it would make
        more client crawls trigger a retry, a behaviour change rather than a measurement fix,
        and it is filed as #107.

        Cloudflare here DELEGATES to it rather than reimplementing the check. The first cut
        duplicated the logic with `or` where the original used `and`, so the two disagreed
        about Cloudflare -- exactly the drift that having one answer avoids.

        None means "no proof an edge refused us", NOT "the origin refused us". Those are
        different claims and the counts should be read as the first."""
        try:
            if int(response.status) not in self.WAF_CHALLENGE_STATUSES:
                return None
            headers = response.headers
            for vendor, generated in self._EDGE_GENERATED_SIGNALS:
                if any(headers.get(h) for h in generated):
                    return vendor
            return "cloudflare" if self._is_waf_challenge(response) else None
        except Exception:
            self.logger.debug("edge-wall vendor detection failed", exc_info=True)
            return None

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
                    is_internal=self.is_same_site,
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

    @staticmethod
    def _is_error_status(status) -> bool:
        """4xx/5xx. A dead URL, whatever the flavour."""
        try:
            return 400 <= int(status) < 600
        except (TypeError, ValueError):
            return False

    # Transport-failure classification (issue #73).
    #
    # These names are VERIFIED against the installed stack, not assumed. Scrapy 2.18 does
    # NOT surface Twisted's exceptions: `scrapy/utils/_download_handlers.py` wraps every one
    # of them (`DNSLookupError` -> `CannotResolveHostError`, `TxTimeoutError` ->
    # `DownloadTimeoutError`, `TxConnectionRefusedError` -> `DownloadConnectionRefusedError`,
    # `CancelledError` -> `DownloadCancelledError`, everything else -> `DownloadFailedError`).
    # A first cut of this table listed the Twisted names and was therefore entirely dead: in
    # production every failure would have classified as `other`, and the per-reason counts
    # this whole chain exists to produce would all have been zero.
    #
    # The `--impersonate` path is a SECOND stack. scrapy-impersonate uses curl_cffi, whose
    # exceptions never pass through Scrapy's wrapper -- and that path is the one used on
    # exactly the Cloudflare/Kinsta-fronted clients where reachability matters most.
    #
    # Matching is still by class NAME so we import neither library's internals, which move
    # between versions. Unknown names degrade to `other`, which is honest -- we saw a
    # failure and cannot name it -- rather than crashing a crawl.
    _TRANSPORT_FAILURE_KINDS = (
        # Scrapy 2.13+ wrapped exceptions
        (("CannotResolveHostError",), "dns"),
        (("DownloadTimeoutError",), "timeout"),
        (("DownloadConnectionRefusedError",), "connection"),
        (("DownloadFailedError", "ResponseDataLossError"), "connection"),
        (("UnsupportedURLSchemeError",), "other"),
        # curl_cffi, via scrapy-impersonate
        (("DNSError",), "dns"),
        (("ConnectTimeout", "ReadTimeout", "Timeout"), "timeout"),
        (("ConnectionError", "ProxyError", "IncompleteRead",
          "ChunkedEncodingError"), "connection"),
        (("CertificateVerifyError", "SSLError"), "tls"),
        # Raw Twisted names, still reachable from code paths that bypass the wrapper (and
        # from a droplet venv older than the requirements floor). Cheap insurance.
        (("DNSLookupError",), "dns"),
        (("TimeoutError", "TCPTimedOutError", "UserTimeoutError"), "timeout"),
        (("CertificateError", "SSLHandshakeError", "OpenSSLError"), "tls"),
        (("ConnectionRefusedError", "ConnectionLost", "ConnectionDone", "ConnectError",
          "ConnectBindError", "ResponseNeverReceived", "ResponseFailed"), "connection"),
    )
    # Never reported as a site failure -- a fix for "we lose failures" must not invent them.
    #
    # `IgnoreRequest` is OUR OWN middleware declining the request (the SSRF guard); blaming
    # the site for our decision would be wrong.
    #
    # A CANCELLATION is us aborting a download that the server was answering fine. The live
    # source is DOWNLOAD_MAXSIZE (64MB): an extensionless download endpoint is fetched as a
    # page, and Scrapy raises `DownloadCancelledError` when the body crosses the cap. The
    # server responded perfectly; we hung up. Reporting that as "the host may be down or
    # gone" is a defect we would be inventing in the client's site.
    #
    # NOTE the earlier justification for this guard -- that a CLOSESPIDER_TIMEOUT close
    # cancels every in-flight download at once -- was wrong. `_Slot.close()` DRAINS
    # in-flight requests rather than cancelling them, and the memusage and SIGTERM paths go
    # through the same close. The guard is still needed, for the size-cap reason above.
    _NOT_A_SITE_FAILURE = (
        "IgnoreRequest", "DownloadCancelledError", "CancelledError",
    )
    @classmethod
    def _classify_transport_failure(cls, exc_name: str) -> str | None:
        """A stable reason token for a transport failure, or None when the failure is not
        the site's fault and must not be reported as coverage."""
        if exc_name in cls._NOT_A_SITE_FAILURE:
            return None
        for names, kind in cls._TRANSPORT_FAILURE_KINDS:
            if exc_name in names:
                return kind
        return "other"

    def page_failed(self, failure):
        """A scheduled request that never produced a response (issue #73).

        DNS failure, refused/reset connection, TLS error or a per-request timeout produce NO
        response, so `parse` never runs and no row is emitted. Scrapy tallies
        `downloader/exception_count`, but with no errback the specific URL and reason were
        lost -- the page simply vanished from the crawl. "We tried to reach N pages and
        couldn't" is real coverage, and it is distinct both from a 4xx/5xx (recorded) and
        from the auth-gated skips (#43).

        Fires only after RetryMiddleware has exhausted its retries, so a transient blip that
        later succeeded is never reported.

        Emitted as a SKIP row, the #43 mechanism: the corpus routes any row carrying a
        skip_reason to excluded_urls and never to page_versions, so an unreachable URL
        surfaces under coverage without ever counting as a page or scoring as a block."""
        request = getattr(failure, "request", None)
        url = getattr(request, "url", "") or ""
        exc_name = type(getattr(failure, "value", failure)).__name__
        kind = self._classify_transport_failure(exc_name)
        if kind is None:
            self._stat("transport_failures_not_reported")
            self.logger.debug("Not a site failure (%s): %s", exc_name, url)
            return
        self._stat("transport_failures")
        self._stat(f"transport_failures/{kind}")
        self.logger.info("Unreachable (%s / %s): %s", kind, exc_name, url)
        if not url:
            return
        # SCHEDULE-normalized, like the `login_gated` sibling in `_schedule` -- NOT
        # emit-normalized. `reach_pagination=1` is always on in run_spider, so emit-mode
        # strips `?page=N`: emitting `/blog/?page=14` as `/blog/` would list a URL that
        # crawled fine under "never responded", and the corpus upsert is keyed on
        # (crawl_id, url) so 18 failed pagination pages would collapse into one row with the
        # wrong name. The identity of the thing that failed is the point of the row.
        schedule_url = self.normalize_url(url, exclude_params=self.exclude_params_schedule)
        self._bind_dedup_state()  # restore first_referrer on a resumed session (#52)
        yield self._skip_row(
            schedule_url,
            f"unreachable_{kind}",
            self.first_referrer.get(self.facet_dedup_key(schedule_url)),
        )

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

    def _schedule(self, url, *, referrer_emit: str | None = None, guessed_source=False):
        """
        Normalize with schedule-mode (pagination retained when reach_pagination=1),
        de-dup, and enqueue the next request. Record the first referrer seen.

        `guessed_source` marks a URL we found in a sitemap WE GUESSED the location of
        (issue #77) rather than one the site pointed us at. See `parse` for why that
        provenance has to survive as far as the emitted row.
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
            # Split assets from pages (issue #74 review). A site with
            # `Disallow: /wp-content/uploads/` and 600 linked PDFs is FULLY inventoried --
            # counting those files as "pages the site withheld" would tell a client their
            # complete report was partial, the same lie as gastro.org's in the opposite
            # direction. Only the page count feeds the withheld-site signal.
            if self.is_asset_url(normalized):
                self.crawler.stats.inc_value("robots_disallowed_assets_skipped")
            else:
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
                errback=self.page_failed,
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
        yield scrapy.Request(
            normalized, callback=self.parse, errback=self.page_failed, dont_filter=True,
            meta={"guessed_source": True} if guessed_source else {},
        )

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
