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

from embed_allowlist import is_benign_host, load_benign_hosts

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

# Elements whose text is never page content; dropped before counting/matching.
_NONCONTENT_TAGS = ("script", "style", "noscript", "template")

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ExtractionResult:
    """The outcome of main-content extraction for one page.

    subtree:                the lxml element to run structural counts over --
                            the located main region when main_content_extracted
                            is True, otherwise the full <body>.
    main_content_extracted: True when counts are scoped to a located main region;
                            False when they fall back to the whole <body>.
    normalized_text:        normalized text used for the content hash -- the
                            extracted main text when trafilatura succeeded,
                            otherwise the normalized <body> text.
    """

    subtree: etree._Element
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


def _locate_main_subtree(
    body_el: etree._Element, main_text: str
) -> etree._Element | None:
    """Find the tightest original-DOM element that holds the main text.

    Scores candidate block containers by the fraction of the main text's word
    tokens they contain; among those clearing the capture threshold, picks the
    one with the fewest total tokens (least surrounding chrome). Returns None
    when no single element captures enough of the main text -- the caller then
    counts over the whole <body>.
    """
    target = set(_tokens(main_text))
    if not target:
        return None

    candidates = body_el.xpath(
        ".//main | .//article | .//*[@role='main'] | .//section | .//div"
    )

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
        "table_count": _count(".//table"),
        "form_count": _count(".//form"),
        "iframe_count": _count(".//iframe"),
        "heading_count": _count(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"),
    }


def embed_signals(
    subtree: etree._Element, benign_hosts: frozenset[str] | None = None
) -> dict:
    """Compute the "surprise embed" signals over ``subtree``.

    Returns ``iframe_hosts`` (distinct iframe hostnames, first-seen order) and
    ``embed_count_nonbenign`` (count of iframe elements whose host is not on the
    benign allowlist). Relative/same-origin iframes (no host) are ignored: the
    signal targets surprising *external* embeds. ``iframe_hosts`` is the durable
    raw signal -- downstream consumers can re-derive their own classification
    from it even if the allowlist changes.
    """
    if benign_hosts is None:
        benign_hosts = load_benign_hosts()

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


def extract_content(body: bytes) -> ExtractionResult:
    """Extract main content from an HTML response body.

    Returns the subtree to count over, whether counts are main-scoped, and the
    normalized text to hash. Pure and per-call: parses one page and retains no
    references afterward.
    """
    body_el = _parse_body(body)
    if body_el is None:
        # Unparseable: nothing to count, empty hash input.
        empty = lxml_html.Element("body")
        return ExtractionResult(empty, main_content_extracted=False, normalized_text="")

    # Oversized bodies skip extraction to bound memory; count over <body>.
    if len(body) > MAX_BODY_BYTES:
        return ExtractionResult(
            body_el,
            main_content_extracted=False,
            normalized_text=normalize_content_text(body_el.text_content()),
        )

    main_text = _extract_main_text(body)
    if main_text is None:
        # Extraction failed/empty: body-scope counts, hash the body text.
        return ExtractionResult(
            body_el,
            main_content_extracted=False,
            normalized_text=normalize_content_text(body_el.text_content()),
        )

    normalized = normalize_content_text(main_text)
    subtree = _locate_main_subtree(body_el, main_text)
    if subtree is None:
        # Extracted text but couldn't pinpoint the region: count over <body>,
        # but still hash the (better) main text.
        return ExtractionResult(
            body_el, main_content_extracted=False, normalized_text=normalized
        )

    return ExtractionResult(
        subtree, main_content_extracted=True, normalized_text=normalized
    )
