"""
Per-page content enrichment: main-content extraction, structural counting,
embed signals, and content hashing.

This module turns an HTML response body into the additive NDJSON fields the
corpus store (yoko-corpus) consumes. It is pure and per-response: it parses one
page, returns counts/signals/hash, and retains nothing across calls.

Design decisions (see docs/plans/...-crawler-corpus-enrichment-plan.md):

- We pass already-downloaded bytes to trafilatura -- never a URL. trafilatura
  performs no network fetch here.
- trafilatura's default 30s extraction timeout uses signal.SIGALRM, which raises
  "signal only works in main thread" inside Scrapy's Twisted reactor. We disable
  it (EXTRACTION_TIMEOUT=0) and bound runaway work with a body-size guard.
- trafilatura's cleaned output strips <form>/<iframe> and uses a normalized tag
  vocabulary, so it is unusable for structural counts. We use trafilatura ONLY
  to (a) extract the main text for hashing and (b) decide the count scope; all
  element counting happens over the ORIGINAL lxml DOM.
"""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

from lxml import etree
from lxml import html as lxml_html
from trafilatura import bare_extraction
from trafilatura.settings import use_config

from embed_allowlist import is_benign_host

# trafilatura config built once and reused per page. EXTRACTION_TIMEOUT=0
# disables the SIGALRM-based timeout (unusable off the main thread in Scrapy).
_TRAFILATURA_CONFIG = use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

# Bodies larger than this skip main-content extraction and fall back to
# body-scoped counting, to stay well within the 384MB per-spider memory cap.
MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MB

# Extracted main text shorter than this is treated as a failed extraction
# (boilerplate-only / thin pages), triggering the body-scope fallback.
MIN_MAIN_TEXT_CHARS = 25

# A candidate container must hold at least this fraction of the main text's
# word tokens to be considered the main region. Tuned heuristic; recorded here
# so it can be recalibrated against real crawls without hunting through logic.
_MAIN_CAPTURE_THRESHOLD = 0.8

# Upper bound on generic (section/div) candidates scored during main-region
# location. Above this, scoring every candidate's text is too costly (div-soup
# pages from WordPress page builders), so we fall back to body-scoped counting.
_MAX_LOCATE_CANDIDATES = 250

# Elements whose text is never page content; dropped before counting/matching.
# `svg` is here so icon/logo markup (and any inline <text>) never inflates word counts.
_NONCONTENT_TAGS = ("script", "style", "noscript", "template", "svg")

# Site chrome dropped from the counting subtree ON THE BODY-FALLBACK PATH ONLY (issue #9):
# when main-content extraction fails we count over the whole <body>, which otherwise counts
# the nav bar, footer, and per-page search box -- inflating word/link/anchor/form counts and
# falsely tripping the complexity thresholds on essentially every page. body_subtree
# (page-wide embed detection) keeps the FULL body, so a footer tracking iframe is still caught.
#
# Two guards keep this from EATING real content -- which for a scoping tool is worse than
# over-counting (a zeroed page reads falsely simple and under-quotes):
#  1. `<article>` is the content-region signal, NOT `<main>` (page builders wrap the WHOLE
#     body in one <main>, which would defeat the strip): chrome inside an <article> is that
#     article's own header/footer/nav (title, byline, TOC) and is kept.
#  2. `_holds_content`: a chrome candidate that actually holds content -- an <article>/<main>
#     descendant, or substantial NON-LINK prose (real nav/footer/search is link-dense,
#     prose-sparse) -- is kept, so a theme misusing <nav>/<aside>/role=contentinfo to wrap
#     real content isn't silently emptied.
_CHROME_TAGS = ("nav", "aside", "header", "footer")
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "search"})
_CONTENT_ANCESTOR_TAGS = frozenset({"article"})
# A chrome block with more non-link prose than this is likely mis-wrapped real content.
_MIN_CHROME_PROSE_WORDS = 25

# Nav-latch handling on the SUCCESS path (issue #13). When a located main region carries at
# least this many non-link prose words it is trusted verbatim -- its own in-content nav/footer
# (TOC, pagination, byline, related links, title + hero image) is content and is counted, so a
# genuine content page is never de-chromed (no under-count; no <article>-ancestry dependence).
# Only a PROSE-THIN located region -- the pathology where trafilatura's "main text" is the nav
# menu, so locate settles on a whole-page wrapper like div.Site-inner on a thin marketing/
# sponsor page -- has its leaked site menus stripped. Same word floor as _MIN_CHROME_PROSE_WORDS.
_MIN_REGION_PROSE_WORDS = _MIN_CHROME_PROSE_WORDS
# Inside a prose-thin region, a nav/aside/header/footer (or navigation/banner/contentinfo/
# search role) block with MORE than this many links is a leaked site menu and is dropped; a
# link-sparse block (a page title + hero image, a one-line byline) is the page's own and kept.
_LEAKED_MENU_LINK_FLOOR = 3

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Single source of truth for the additive enrichment field names and their
# order. website_spider builds its zero/empty defaults from this; run_spider
# builds FEED_EXPORT_FIELDS from it; tests derive their expectations from it.
# Adding a field here (plus producing it in count_structure/embed_signals)
# propagates everywhere -- no parallel lists to keep in sync. content_text is
# intentionally NOT here: it is conditional on --emit-content.
ENRICHMENT_FIELD_NAMES = (
    "content_hash",
    # Coarse structural fingerprint of the page's layout (issue #36): pages built from the same
    # template share it regardless of content, so the corpus can cluster "N pages, ~M templates".
    "structure_hash",
    "main_content_extracted",
    "word_count",
    "link_count",
    "internal_link_count",
    "external_link_count",
    "pdf_link_count",
    "asset_link_count",
    "anchor_link_count",
    "image_count",
    # Images in the counted content region that are NOT inside a slider/carousel container
    # (issue #25). Distinguishes a media-heavy page (asset/layout lift) from one whose images
    # merely fill a slider (interactive dev lift, counted by slider_count) -- so a carousel's
    # images don't read as "image heavy". Equals image_count whenever no slider sits INSIDE the
    # counted region (slider_count is page-wide, so a slider outside the content region -- e.g.
    # a hero carousel in stripped chrome -- leaves standalone_image_count == image_count).
    "standalone_image_count",
    "table_count",
    "form_count",
    "iframe_count",
    "heading_count",
    "embed_count_nonbenign",
    # Count of DISTINCT non-benign external <script src> hosts (issue #28): third-party
    # integrations (chat, forms-as-a-service, booking, donation, CRM tags, social) that inject
    # via a script rather than an iframe -- the surprise-integration blind spot. Routine hosts
    # (analytics/CDN/fonts) and the site's own scripts are excluded.
    "script_embed_count_nonbenign",
    # Count of interactive JS components (sliders/carousels/accordions/tabs/galleries/
    # lightboxes) detected by container markers (issue #12). Real design+dev work that is
    # otherwise invisible (JS-hydrated) or laundered into word/image counts.
    "component_count",
    # Count of image sliders/carousels specifically (the slider subset of component_count,
    # issue #25) -- interactive component work, kept distinct from a plain wall of images.
    "slider_count",
    "iframe_hosts",
    # Distinct NON-BENIGN external <script src> hostnames (issue #28), first-seen order = the
    # third-party integrations themselves (chat/forms/booking/CRM/social), so downstream can
    # LIST them. script_embed_count_nonbenign == len(script_hosts). Excludes own + routine hosts.
    "script_hosts",
    # The page's <link rel="canonical"> target, normalized (issue #10). "" when absent.
    # Downstream (yoko-corpus) uses it to collapse query-string/pagination/variant URLs to
    # their canonical page. Populated by the spider (it reads the response's <head>), not by
    # the body-scoped extraction here.
    "canonical",
)


def empty_enrichment() -> dict:
    """A fresh zero/empty enrichment dict for rows with no HTML body (assets
    fetched HEAD-only, non-HTML responses, redirects, oversized/unparseable
    bodies). Returns a new dict and a new iframe_hosts list every call."""
    fields = {name: 0 for name in ENRICHMENT_FIELD_NAMES}
    fields["content_hash"] = ""
    fields["structure_hash"] = ""
    fields["main_content_extracted"] = False
    fields["iframe_hosts"] = []
    fields["script_hosts"] = []
    fields["canonical"] = ""
    return fields


@dataclass
class ExtractionResult:
    """The outcome of main-content extraction for one page.

    subtree:                the lxml element to run structural counts over --
                            the located main region when main_content_extracted
                            is True, otherwise the full <body>.
    body_subtree:           the full <body> element, used for page-wide signals
                            (iframe hosts / surprise embeds live anywhere on the
                            page, not just the main region).
    main_content_extracted: True when counts are scoped to a located main region;
                            False when they fall back to the whole <body>.
    normalized_text:        normalized text used for the content hash -- the
                            extracted main text when trafilatura succeeded,
                            otherwise the normalized <body> text (empty for
                            oversized/unparseable bodies).
    """

    subtree: etree._Element
    body_subtree: etree._Element
    main_content_extracted: bool
    normalized_text: str


def normalize_content_text(text: str) -> str:
    """Normalize text for stable hashing across runs and across the
    crawler/corpus boundary.

    Pipeline (fixed -- changing it is a hash-epoch change): Unicode NFC ->
    normalize line endings -> collapse all whitespace runs to a single space ->
    strip. Case is preserved deliberately.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def content_hash(normalized_text: str) -> str:
    """SHA-256 hex of already-normalized content text. Empty text hashes to an
    empty string, matching the asset-row default ("" means no content), so an
    empty page is unambiguous rather than a hash of the empty string.
    """
    if not normalized_text:
        return ""
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


# Coarse structural fingerprint (issue #36) -- clusters pages into distinct TEMPLATES.
_SKELETON_MAX_DEPTH = 4
# Inline / text-carrying tags dropped from the skeleton: they are content, not layout, so
# keeping them would let content differences split pages of the same template.
_SKELETON_SKIP_TAGS = frozenset({
    "a", "span", "b", "i", "em", "strong", "small", "sub", "sup", "u", "mark", "abbr",
    "cite", "q", "br", "wbr", "img", "picture", "source", "svg", "label", "time", "code",
    "bdi", "bdo", "font", "s", "del", "ins",
})
# Site chrome skipped when fingerprinting: page-invariant furniture that would only blur
# distinct templates together (it dominates the skeleton once content sinks below the depth
# budget). Excluding it makes the fingerprint about the CONTENT template, not the theme.
# NOTE: <aside> is intentionally NOT here -- unlike nav/header/footer it is frequently CONTENT
# (in-article TOC, related-posts rail, docs sidebar) and a real template discriminator, so a
# sidebar-vs-no-sidebar difference must split, not merge.
_CHROME_SKELETON_TAGS = frozenset({"nav", "header", "footer"})


def _structural_children(el: etree._Element) -> list:
    """Element children that carry layout: real tags, excluding inline/text tags and site
    chrome (nav/header/footer/aside)."""
    out = []
    for child in el:
        tag = child.tag
        if not isinstance(tag, str):
            continue  # comments / processing instructions
        tag = tag.lower()
        if tag in _SKELETON_SKIP_TAGS or tag in _CHROME_SKELETON_TAGS:
            continue
        out.append(child)
    return out


def _content_root(body_el: etree._Element) -> etree._Element:
    """Descend from <body> past site chrome and pure single-child WRAPPER elements to the real
    content container, so the depth budget is spent on content structure rather than the
    theme/page-builder/SPA nesting boilerplate that wraps it (div#page > div.site-inner >
    div.builder > main). Without this, on any wrapped site the content sinks below the depth
    cutoff and every template collapses to one chrome-only hash -- a silent false-merge.

    Descends only while there is exactly ONE structural child AND that child is itself a
    container (has structural children of its own), so it never descends into a leaf and always
    lands on the branching content root. Known coarse-pass limitation: two templates that differ
    ONLY by a single-child semantic wrapper with identical inner structure (a bare <section> vs
    <article> around the same content) descend to the same inner root and merge -- narrow, and
    accepted for the first pass."""
    cur = body_el
    for _ in range(64):  # generous bound; real theme/builder nesting is a few levels deep
        kids = _structural_children(cur)
        if len(kids) == 1 and _structural_children(kids[0]):
            cur = kids[0]
        else:
            break
    return cur


def _skeleton(el: etree._Element, depth: int) -> str:
    """A depth-limited, content-free string of ``el``'s structural children (chrome + inline
    excluded). Consecutive identical child tokens (tag + their own subtree) collapse to one, so
    a listing of 10 vs 12 identical items -- or a gallery with N images -- produces the SAME
    skeleton. Text and attributes are dropped entirely."""
    if depth > _SKELETON_MAX_DEPTH:
        return ""
    parts: list[str] = []
    prev: str | None = None
    for child in _structural_children(el):
        sub = _skeleton(child, depth + 1)
        token = f"{child.tag.lower()}({sub})" if sub else child.tag.lower()
        if token != prev:  # collapse consecutive identical runs (repeated cards / list items)
            parts.append(token)
            prev = token
    return ",".join(parts)


def structure_hash(body_el: etree._Element) -> str:
    """A content-independent fingerprint of the page's coarse LAYOUT skeleton (issue #36):
    the block-level tag tree of the CONTENT ROOT (chrome and wrapper boilerplate skipped),
    depth-limited, with consecutive identical sibling-runs collapsed and all text/attributes
    dropped. Pass the full <body> -- it locates the content root itself, and unlike the
    trafilatura-located region the body is content-length-stable. Pages built from the same
    template hash identically regardless of their content or how many items they list -- so
    downstream can cluster pages into "N pages across ~M distinct templates".

    Coarse EXACT-hash, first pass (best-guess per issue #36): it errs toward over-splitting
    (an extra structural block -> its own cluster), the safe direction, and is tuned against
    real crawls. Empty when the content root has no structural children (asset/empty page)."""
    if body_el is None:
        return ""
    root = _content_root(body_el)
    skeleton = _skeleton(root, 1)
    if not skeleton:
        return ""  # no structural content -- no fingerprint (excluded from clustering)
    root_tag = root.tag.lower() if isinstance(root.tag, str) else "x"
    return hashlib.sha256(f"{root_tag}({skeleton})".encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    """Lowercased word tokens, used only for main-region matching."""
    return _WORD_RE.findall(text.lower())


def _parse_body(body: bytes) -> etree._Element | None:
    """Parse HTML bytes and return the <body> element (or the root when there is
    no <body>). Non-content elements (script/style/noscript/template) are dropped
    so they never pollute word counts or main-region matching. Returns None when
    the document cannot be parsed into any element.
    """
    try:
        root = lxml_html.fromstring(body)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None
    if root is None:
        return None

    for tag in _NONCONTENT_TAGS:
        for el in root.iter(tag):
            el.drop_tree()

    body_el = root.find(".//body")
    return body_el if body_el is not None else root


def _within_content(el: etree._Element) -> bool:
    """True when `el` is inside an <article> -- an article's own header/footer/nav (title,
    byline, table-of-contents) is content, not chrome, so it must not be stripped. `<main>`
    is deliberately NOT a content signal here: page builders wrap the entire body in one
    <main>, so exempting it would keep the site header/footer and defeat the strip."""
    for ancestor in el.iterancestors():
        if isinstance(ancestor.tag, str) and ancestor.tag.lower() in _CONTENT_ANCESTOR_TAGS:
            return True
    return False


def _prose_word_count(el: etree._Element) -> int:
    """Words of `el`'s text that are NOT inside a link. Real nav/footer/search is link-dense
    with little prose; a block of real content wrapped in a chrome tag has prose."""
    total = len(_WORD_RE.findall(" ".join(el.itertext())))
    link_words = sum(len(_WORD_RE.findall(" ".join(a.itertext()))) for a in el.iterfind(".//a"))
    return max(0, total - link_words)


def _holds_content(el: etree._Element) -> bool:
    """True when a chrome-tagged/role'd element actually holds main content (a theme misusing
    a chrome element to wrap real content), so de-chroming must NOT drop it and zero out a
    real page. Signals: an <article>/<main> descendant, or substantial non-link prose."""
    if el.find(".//article") is not None or el.find(".//main") is not None:
        return True
    return _prose_word_count(el) >= _MIN_CHROME_PROSE_WORDS


def _is_chrome(el: etree._Element) -> bool:
    """Whether `el` is site chrome to drop on the body-fallback path: a nav/aside/header/
    footer tag or a navigation/banner/contentinfo/search ARIA role, that is NOT inside an
    <article> and does NOT itself hold real content."""
    if not isinstance(el.tag, str):
        return False  # comments / processing instructions
    tag = el.tag.lower()
    role = (el.get("role") or "").strip().lower()
    if tag not in _CHROME_TAGS and role not in _CHROME_ROLES:
        return False
    return not _within_content(el) and not _holds_content(el)


def _dechrome(body_el: etree._Element) -> etree._Element:
    """Return a COPY of `body_el` with site chrome removed, for structural counting on the
    body-fallback path (issue #9). See the chrome constants above for the tag/role set and
    the two content-preservation guards. Operates on a deep copy so the caller's
    `body_subtree` (page-wide embed detection) is untouched. Collect-then-drop with a parent
    check so dropping an ancestor first doesn't double-drop a nested match."""
    clone = copy.deepcopy(body_el)
    to_drop = [el for el in clone.iter() if _is_chrome(el)]
    for el in to_drop:
        if el.getparent() is not None:  # skip anything already removed with an ancestor
            el.drop_tree()
    return clone


def _is_leaked_menu(el: etree._Element) -> bool:
    """Whether `el` is a leaked site menu inside a PROSE-THIN located region (issue #13): a
    nav/aside/header/footer tag or navigation/banner/contentinfo/search role that is link-dense
    (> _LEAKED_MENU_LINK_FLOOR links) AND holds no non-menu content of its own. Deliberately
    link-count based and ancestry-FREE: it runs on a region already known to carry almost no
    prose of its own, so there is no real in-content navigation to protect, and being
    ancestry-free it is immune to the deepcopy that severs an <article> parent.

    A block that holds its OWN title/media/prose -- a heading, an <img>, or non-link prose --
    is a CONTAINER (e.g. a hero `<header>` wrapping the site nav), not a pure menu: it is kept
    so its title + hero image survive, and its nested `<nav>` is dropped on its own (it matches
    _is_leaked_menu independently). A link-sparse header/footer stays for the same reason."""
    if not isinstance(el.tag, str):
        return False  # comments / processing instructions
    tag = el.tag.lower()
    role = (el.get("role") or "").strip().lower()
    if tag not in _CHROME_TAGS and role not in _CHROME_ROLES:
        return False
    if int(el.xpath("count(.//a[@href])")) <= _LEAKED_MENU_LINK_FLOOR:
        return False  # link-sparse: the page's own header/footer, not a menu
    # Link-dense, but a heading / image / real prose of its own means it merely WRAPS a menu
    # (drop that nested menu instead) rather than being one -- don't take its title/hero with it.
    if el.xpath("count(.//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//img)") > 0:
        return False
    return _prose_word_count(el) < _MIN_CHROME_PROSE_WORDS


def _dechrome_menus(subtree: etree._Element) -> etree._Element:
    """Return a COPY of a PROSE-THIN located `subtree` with its leaked site menus removed
    (issue #13). Unlike _dechrome (body-fallback path), this strips only link-dense chrome
    (_is_leaked_menu) and keeps link-sparse header/footer, so a thin page's title + hero image
    survive while a swept-in nav/footer menu does not. Deep copy so `body_subtree` is untouched;
    parent check so a nested menu isn't double-dropped."""
    clone = copy.deepcopy(subtree)
    to_drop = [el for el in clone.iter() if _is_leaked_menu(el)]
    for el in to_drop:
        if el.getparent() is not None:
            el.drop_tree()
    return clone


def _extract_main_text(body: bytes) -> str | None:
    """Return trafilatura's main text for the page, or None on failure/empty.

    fast=True skips the heavier fallback extractors; we only need region
    identification plus a success signal. Comments are excluded so the hash
    tracks main content, not discussion churn.
    """
    try:
        doc = bare_extraction(
            body,
            fast=True,
            include_comments=False,
            include_tables=True,
            config=_TRAFILATURA_CONFIG,
        )
    except Exception:
        # trafilatura/lxml can raise on pathological markup; treat as failure.
        return None
    if doc is None:
        return None
    # bare_extraction returns a Document in 2.x (dict when as_dict=True); support
    # both so a minor API shift doesn't silently disable extraction.
    text = doc.get("text") if isinstance(doc, dict) else getattr(doc, "text", None)
    if not text or len(text.strip()) < MIN_MAIN_TEXT_CHARS:
        return None
    return text


def _best_candidate(
    candidates: list, target: set
) -> etree._Element | None:
    """Among candidate elements, return the one with the fewest tokens that
    still captures >= the threshold fraction of the target tokens (tightest fit,
    least surrounding chrome). None if none clear the threshold."""
    best_el = None
    best_token_count = None
    for el in candidates:
        el_tokens = _tokens(el.text_content())
        if not el_tokens:
            continue
        captured = len(target & set(el_tokens)) / len(target)
        if captured < _MAIN_CAPTURE_THRESHOLD:
            continue
        total = len(el_tokens)
        if best_token_count is None or total < best_token_count:
            best_el = el
            best_token_count = total
    return best_el


def _locate_main_subtree(
    body_el: etree._Element, main_text: str
) -> etree._Element | None:
    """Find the tightest original-DOM element that holds the main text.

    Semantic containers (<main>/<article>/[role=main]) are scored first -- they
    are few and usually correct, so the common case skips the expensive generic
    scan entirely. Only when no semantic container qualifies do we score generic
    block containers (section/div), and that scan is bounded: div-soup pages
    (WordPress page builders) can yield hundreds of candidates, each costing a
    full text materialization, so above _MAX_LOCATE_CANDIDATES we give up and
    let the caller count over the whole <body>. Returns None when no single
    element captures enough of the main text.
    """
    target = set(_tokens(main_text))
    if not target:
        return None

    semantic = body_el.xpath(".//main | .//article | .//*[@role='main']")
    best = _best_candidate(semantic, target)
    if best is not None:
        return best

    generic = body_el.xpath(".//section | .//div")
    if len(generic) > _MAX_LOCATE_CANDIDATES:
        return None  # too many to score affordably; count over <body>
    return _best_candidate(generic, target)


def _href_path(resolved_url: str) -> str:
    """Lowercased path of a resolved URL, fragment/query stripped."""
    return (urlparse(resolved_url).path or "").lower()


def _is_anchor_link(href: str, page_url: str) -> bool:
    """True for in-page jump links: a bare ``#frag`` href, or one that resolves
    to the current page URL plus a fragment.
    """
    if href.startswith("#"):
        return True
    resolved = urljoin(page_url, href)
    base, sep, frag = resolved.partition("#")
    if not sep or not frag:
        return False
    return base == page_url.partition("#")[0]


def count_structure(
    subtree: etree._Element,
    page_url: str,
    *,
    is_internal: Callable[[str], bool],
    asset_extensions: set[str],
) -> dict[str, int]:
    """Compute the structural counts for one page over ``subtree``.

    ``is_internal`` and ``asset_extensions`` come from the spider so the
    internal/external split and asset classification stay consistent with how
    the crawler already classifies URLs.
    """
    # Join via itertext (not text_content) so adjacent block elements with no
    # whitespace between them in source -- e.g. <h2>A</h2><h3>B</h3> -- don't
    # merge into a single token.
    word_count = len(_WORD_RE.findall(" ".join(subtree.itertext())))

    link_count = internal_link_count = pdf_link_count = 0
    asset_link_count = anchor_link_count = 0
    for a in subtree.xpath(".//a[@href]"):
        href = a.get("href", "")
        if not href:
            continue
        link_count += 1
        if _is_anchor_link(href, page_url):
            anchor_link_count += 1
        resolved = urljoin(page_url, href)
        if is_internal(resolved):
            internal_link_count += 1
        path = _href_path(resolved)
        if path.endswith(".pdf"):
            pdf_link_count += 1
        if any(path.endswith(ext) for ext in asset_extensions):
            asset_link_count += 1

    def _count(expr: str) -> int:
        return int(subtree.xpath(f"count({expr})"))

    return {
        "word_count": word_count,
        "link_count": link_count,
        "internal_link_count": internal_link_count,
        "external_link_count": link_count - internal_link_count,
        "pdf_link_count": pdf_link_count,
        "asset_link_count": asset_link_count,
        "anchor_link_count": anchor_link_count,
        "image_count": _count(".//img"),
        # Images not inside a slider/carousel container (issue #25): the media-heavy signal,
        # so a carousel's images aren't double-counted as standalone page images.
        "standalone_image_count": _count(_STANDALONE_IMG_XPATH),
        "table_count": _count(".//table"),
        "form_count": _count(".//form"),
        "iframe_count": _count(".//iframe"),
        "heading_count": _count(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"),
    }


# Container class tokens for common interactive JS components (issue #12). Matched as
# whitespace-separated CLASS TOKENS (not substrings) so a slider's child parts
# (`swiper-slide`, `carousel-item`) don't inflate the count -- only the container matches.
_COMPONENT_CLASS_TOKENS = (
    "swiper", "swiper-container", "slick-slider", "carousel", "splide", "owl-carousel",
    "flickity-enabled", "flexslider", "tns-slider", "accordion", "lightbox", "fancybox",
    "galleria", "lightgallery", "masonry", "nav-tabs",
)
# CONTAINER-level attribute markers only. Per-ITEM markers (`data-fancybox` on each gallery
# link, `data-toggle=tab` on each tab button) are deliberately excluded -- they'd count once
# per trigger, inflating one gallery/tab set into N components. Tab widgets are still caught
# by the container (`nav-tabs` class + `role=tablist`); a bare per-link Fancybox gallery with
# no container class is missed (acceptable -- a miss beats a 10x over-count for a scoping
# signal, and class-based galleries lightbox/galleria/lightgallery/masonry are still caught).
_COMPONENT_ATTR_PREDICATES = (
    "@data-slider", "@data-carousel", "@role='tablist'",
)
# descendant-or-self so the passed subtree is counted if it is itself a component container
# (robust regardless of what element the caller passes).
_COMPONENT_XPATH = "descendant-or-self::*[" + " or ".join(
    [f"contains(concat(' ', normalize-space(@class), ' '), ' {t} ')" for t in _COMPONENT_CLASS_TOKENS]
    + list(_COMPONENT_ATTR_PREDICATES)
) + "]"


def component_signals(subtree: etree._Element) -> dict:
    """Count interactive JS components (sliders/carousels/accordions/tabs/galleries/
    lightboxes) over ``subtree`` (the full page <body> -- a slider lives anywhere). Matches
    a curated set of CONTAINER class tokens + attribute markers; distinct matched elements
    are counted (xpath returns a unique node set), so one component with several markers
    counts once and a slider's child slides don't count at all. Detection is over the raw
    pre-JS HTML, so it catches components declared in markup (the common case for these
    libraries) even though they hydrate client-side. issue #12."""
    return {"component_count": len(subtree.xpath(_COMPONENT_XPATH))}


# The SLIDER/CAROUSEL subset of the component tokens (issue #25): image sliders specifically,
# NOT accordions/tabs/lightboxes/galleries/masonry. Kept as a strict subset of
# _COMPONENT_CLASS_TOKENS / _COMPONENT_ATTR_PREDICATES so a slider always counts in BOTH
# slider_count and component_count. Same class-token matching (whitespace-separated, so child
# `swiper-slide`/`carousel-item` parts never match -- only the container).
_SLIDER_CLASS_TOKENS = (
    "swiper", "swiper-container", "slick-slider", "carousel", "splide", "owl-carousel",
    "flickity-enabled", "flexslider", "tns-slider",
)
_SLIDER_ATTR_PREDICATES = ("@data-slider", "@data-carousel")
# The boolean "is a slider container" test, reused two ways below.
_SLIDER_MATCH = " or ".join(
    [f"contains(concat(' ', normalize-space(@class), ' '), ' {t} ')" for t in _SLIDER_CLASS_TOKENS]
    + list(_SLIDER_ATTR_PREDICATES)
)
# descendant-or-self so the passed subtree counts if it is itself a slider container.
_SLIDER_XPATH = f"descendant-or-self::*[{_SLIDER_MATCH}]"
# Images NOT inside any slider container -- the "standalone" (non-carousel) images.
_STANDALONE_IMG_XPATH = f".//img[not(ancestor::*[{_SLIDER_MATCH}])]"


def slider_signals(subtree: etree._Element) -> dict:
    """Count image sliders/carousels over ``subtree`` (the full page <body> -- a slider lives
    anywhere, like component detection). A strict subset of component_signals: matches only the
    slider/carousel container tokens, so distinct slider containers are counted once each and
    their child slides never inflate it. Interactive component work, kept apart from a plain
    wall of images so the two deployment lifts read differently downstream. issue #25."""
    return {"slider_count": len(subtree.xpath(_SLIDER_XPATH))}


def embed_signals(
    subtree: etree._Element, benign_hosts: frozenset[str]
) -> dict:
    """Compute the "surprise embed" signals over ``subtree`` (the full page
    <body> -- surprising embeds live anywhere, not just the main region).

    Returns ``iframe_hosts`` (distinct iframe hostnames, first-seen order) and
    ``embed_count_nonbenign`` (count of iframe elements whose host is not on the
    benign allowlist). Relative/same-origin iframes (no host) are ignored: the
    signal targets surprising *external* embeds. ``iframe_hosts`` is the durable
    raw signal -- downstream consumers can re-derive their own classification
    from it even if the allowlist changes. ``benign_hosts`` is required (resolve
    it once per crawl via load_benign_hosts) so this stays a pure function with
    no hidden environment read.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    nonbenign = 0
    for iframe in subtree.xpath(".//iframe[@src]"):
        src = iframe.get("src", "").strip()
        if not src:
            continue
        host = (urlparse(src).hostname or "").lower().rstrip(".")
        if not host:
            continue  # relative/same-origin embed -- not a surprise
        if host not in seen:
            seen.add(host)
            hosts.append(host)
        if not is_benign_host(host, benign_hosts):
            nonbenign += 1

    return {"iframe_hosts": hosts, "embed_count_nonbenign": nonbenign}


def script_signals(
    body: bytes,
    benign_hosts: frozenset[str],
    self_hosts: frozenset[str],
) -> dict:
    """Compute the "third-party integration" signals from external ``<script src>`` hosts
    across the whole page (issue #28).

    Most third-party functionality (chat, forms-as-a-service, booking, donation, CRM tags,
    social embeds) injects via a script tag, not an iframe -- the crawler was blind to all of
    it. Returns ``script_hosts`` (distinct NON-BENIGN external script hostnames = the actual
    third-party integrations, first-seen order) and ``script_embed_count_nonbenign`` (=
    len(script_hosts) -- "how many separate integrations", not how many script tags).

    We record only the non-benign hosts (not benign jQuery/GA noise) so the list IS the
    integration list downstream needs, with no allowlist required corpus-side. This is a
    DELIBERATE divergence from iframe_hosts (which records ALL hosts for re-classification):
    the tradeoff is that if the allowlist changes, a re-crawl -- not old data -- reflects it.
    That fits a re-crawling scoping tool, and keeps benign CDN noise out of the report. Takes
    the RAW
    body bytes and parses them fresh, because _parse_body drops <script> tags (to keep word
    counts clean) -- so the stripped body_subtree has no scripts to scan. Scans <head> and
    <body>. ``self_hosts`` (the site's own base + www) are its own code, not an integration;
    ``benign_hosts`` (analytics/CDN/fonts) are routine infrastructure. Both are excluded. Both
    sets are passed in (resolved once per crawl) so this stays a pure, env-free function.
    """
    empty = {"script_hosts": [], "script_embed_count_nonbenign": 0}
    if not body or len(body) > MAX_BODY_BYTES:
        return empty
    try:
        root = lxml_html.fromstring(body)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return empty
    if root is None:
        return empty

    hosts: list[str] = []
    seen: set[str] = set()
    for script in root.iter("script"):
        src = (script.get("src") or "").strip()
        if not src:
            continue
        host = (urlparse(src).hostname or "").lower().rstrip(".")
        if not host:
            continue  # relative / same-origin path -- not a third-party host
        if is_benign_host(host, self_hosts) or is_benign_host(host, benign_hosts):
            continue  # the site's own code, or routine infrastructure -- not an integration
        if host not in seen:
            seen.add(host)
            hosts.append(host)

    return {"script_hosts": hosts, "script_embed_count_nonbenign": len(hosts)}


def extract_content(body: bytes) -> ExtractionResult:
    """Extract main content from an HTML response body.

    Returns the subtree to count over, whether counts are main-scoped, and the
    normalized text to hash. Pure and per-call: parses one page and retains no
    references afterward.
    """
    # Oversized bodies are skipped BEFORE any lxml parse -- parsing first would
    # allocate a DOM several times the raw byte size and defeat the memory guard.
    # An oversized body yields empty enrichment (no counts, empty hash).
    if len(body) > MAX_BODY_BYTES:
        empty = lxml_html.Element("body")
        return ExtractionResult(empty, empty, False, "")

    body_el = _parse_body(body)
    if body_el is None:
        # Unparseable: nothing to count, empty hash input.
        empty = lxml_html.Element("body")
        return ExtractionResult(empty, empty, False, "")

    main_text = _extract_main_text(body)
    if main_text is None:
        # Extraction failed/empty: body-scope counts over a DE-CHROMED copy (issue #9) so
        # nav/footer/search-box don't inflate the counts, but hash the full body text and
        # keep the full body for page-wide embed detection.
        return ExtractionResult(
            _dechrome(body_el), body_el, False, normalize_content_text(body_el.text_content())
        )

    normalized = normalize_content_text(main_text)
    subtree = _locate_main_subtree(body_el, main_text)
    if subtree is None:
        # Extracted text but couldn't pinpoint the region: count over the de-chromed <body>
        # (issue #9), but still hash the (better) main text.
        return ExtractionResult(_dechrome(body_el), body_el, False, normalized)

    if _prose_word_count(subtree) >= _MIN_REGION_PROSE_WORDS:
        # The located region carries real prose of its OWN: trust it verbatim. Its in-content
        # nav/footer/header (TOC, pagination, byline, related links, title, hero image) is
        # content and is counted -- we never de-chrome a genuine content region, so there is no
        # under-count and no dependence on <article> ancestry (which a deepcopy would sever).
        return ExtractionResult(subtree, body_el, True, normalized)

    # Prose-thin located region (issue #13): trafilatura's "main text" was the nav menu, so
    # locate settled on a whole-page wrapper (e.g. `div.Site-inner` on a thin marketing/sponsor
    # page) that is almost entirely links. Counting it verbatim reports the site nav/footer as
    # page content and falsely trips many_links / image_heavy. Strip only the link-dense menus
    # it swept in (keeping a link-sparse title/hero header) rather than de-chroming a real
    # region -- so the fix never touches genuine content pages.
    return ExtractionResult(_dechrome_menus(subtree), body_el, True, normalized)
