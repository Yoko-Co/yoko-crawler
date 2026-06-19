"""Tests for content_extractor: main-content extraction + scoping (U1)."""

from __future__ import annotations

from urllib.parse import urlparse

from lxml import html as lxml_html

import content_extractor as ce
from content_extractor import (
    ExtractionResult,
    content_hash,
    count_structure,
    embed_signals,
    extract_content,
    normalize_content_text,
)
from website_spider import WebsiteSpider

# The real asset-extension set, so tests classify links exactly as the spider does.
ASSET_EXTS = WebsiteSpider.ASSET_EXTENSIONS


def _internal(url: str) -> bool:
    """Test stand-in for the spider's is_internal: example.com (+ www) or relative."""
    return urlparse(url).hostname in (None, "", "example.com", "www.example.com")

# A realistic article page: small nav/header/footer chrome around a substantial
# <main> region. Marker words let tests assert which region was scoped.
ARTICLE_HTML = b"""
<!DOCTYPE html>
<html lang="en">
<head><title>Tide Pools of the Northern Coast</title></head>
<body>
  <header><a href="/">NAVHOME</a> <a href="/about">NAVABOUT</a></header>
  <nav><a href="/news">NAVNEWS</a> <a href="/contact">NAVCONTACT</a></nav>
  <main>
    <article>
      <h1>Tide Pools of the Northern Coast</h1>
      <p>The rocky shelves along the northern coast hold dozens of tide pools
         that fill and drain with each turning of the sea. At low water the
         pools reveal anemones, hermit crabs, and the occasional sculpin
         waiting out the ebb in a pocket of cold clear water.</p>
      <p>Naturalists have catalogued the pools for over a century, returning
         season after season to record which species persist and which fade.
         The longest running survey began in the autumn of eighteen ninety and
         has continued, with gaps, into the present decade.</p>
      <p>Visitors are asked to tread lightly. A single careless step can crush
         a colony that took years to establish, and the cumulative weight of a
         summer of footsteps reshapes the pools more than any storm.</p>
      <p>The best time to visit is the hour before the lowest tide, when the
         water has drained but the creatures have not yet retreated into the
         deeper crevices beyond the reach of curious hands and cameras.</p>
    </article>
  </main>
  <footer>FOOTERCOPYRIGHT all rights reserved</footer>
</body>
</html>
"""


class TestExtractContentHappyPath:
    def test_extracts_and_scopes_to_main_region(self):
        result = extract_content(ARTICLE_HTML)
        assert isinstance(result, ExtractionResult)
        assert result.main_content_extracted is True
        scoped_text = result.subtree.text_content()
        # Main article text is present...
        assert "tide pools" in scoped_text.lower()
        # ...but nav and footer chrome are excluded from the counted region.
        assert "NAVHOME" not in scoped_text
        assert "NAVNEWS" not in scoped_text
        assert "FOOTERCOPYRIGHT" not in scoped_text

    def test_normalized_text_is_main_content(self):
        result = extract_content(ARTICLE_HTML)
        assert "tide pools" in result.normalized_text.lower()
        # Chrome words do not leak into the hashed text.
        assert "FOOTERCOPYRIGHT" not in result.normalized_text


class TestExtractContentFallback:
    def test_boilerplate_only_falls_back_to_body(self):
        # No real main content -> trafilatura returns nothing usable.
        html = b"<html><body><div><a href='/x'>link</a></div></body></html>"
        result = extract_content(html)
        assert result.main_content_extracted is False
        # Subtree is the body, so its text is available for body-scoped counting.
        assert result.subtree is not None

    def test_short_main_text_triggers_fallback(self):
        html = b"<html><body><main><p>Too short.</p></main></body></html>"
        result = extract_content(html)
        assert result.main_content_extracted is False

    def test_unparseable_body_yields_empty_result(self):
        result = extract_content(b"")
        assert result.main_content_extracted is False
        assert result.normalized_text == ""

    def test_oversized_body_skips_extraction(self, monkeypatch):
        # A body over the size guard counts over <body> without invoking
        # trafilatura, and still produces hashable text. Force the guard with a
        # low threshold rather than allocating a multi-MB payload.
        monkeypatch.setattr(ce, "MAX_BODY_BYTES", 64)
        result = extract_content(ARTICLE_HTML)
        assert len(ARTICLE_HTML) > ce.MAX_BODY_BYTES
        assert result.main_content_extracted is False
        assert result.normalized_text  # non-empty body text
        # Body-scoped text includes chrome that main-scoping would have dropped.
        assert "footercopyright" in result.normalized_text.lower()


class TestNormalizeContentText:
    def test_collapses_whitespace_and_strips(self):
        assert normalize_content_text("  a\t\n  b   c \n") == "a b c"

    def test_identical_content_normalizes_identically(self):
        a = normalize_content_text("Hello   world\nfoo")
        b = normalize_content_text("Hello world foo")
        assert a == b

    def test_whitespace_only_change_is_insignificant(self):
        base = "The quick brown fox"
        spaced = "The   quick\tbrown\r\nfox"
        assert normalize_content_text(base) == normalize_content_text(spaced)

    def test_case_is_preserved(self):
        assert normalize_content_text("Apple") != normalize_content_text("apple")

    def test_meaningful_change_differs(self):
        assert normalize_content_text("cat sat") != normalize_content_text("cat ran")

    def test_empty_input(self):
        assert normalize_content_text("") == ""


# A fragment with a known, hand-counted mix of structural elements.
COUNT_FRAGMENT = """
<div>
  <p>alpha beta gamma</p>
  <a href="/internal-page"></a>
  <a href="https://other.org/external"></a>
  <a href="/docs/report.pdf"></a>
  <a href="/files/archive.zip"></a>
  <a href="#section-two"></a>
  <a href="https://example.com/articles/tide#deep"></a>
  <img src="a.png"><img src="b.png">
  <table><tr><td>cell</td></tr></table>
  <form><input name="q"></form>
  <iframe src="https://www.youtube.com/embed/x"></iframe>
  <h2>Title</h2><h3>Sub</h3>
</div>
"""

PAGE_URL = "https://example.com/articles/tide"


class TestCountStructure:
    def _counts(self):
        subtree = lxml_html.fromstring(COUNT_FRAGMENT)
        return count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )

    def test_link_counts(self):
        c = self._counts()
        assert c["link_count"] == 6
        # five internal (relative, both anchors, pdf, zip), one external (other.org)
        assert c["internal_link_count"] == 5
        assert c["external_link_count"] == 1

    def test_pdf_and_asset_counts(self):
        c = self._counts()
        # .pdf counts as both pdf and asset; .zip is asset-only.
        assert c["pdf_link_count"] == 1
        assert c["asset_link_count"] == 2

    def test_anchor_counts_both_forms(self):
        c = self._counts()
        # "#section-two" and the absolute self-URL-plus-fragment both count.
        assert c["anchor_link_count"] == 2

    def test_element_counts(self):
        c = self._counts()
        assert c["image_count"] == 2
        assert c["table_count"] == 1
        assert c["form_count"] == 1
        assert c["iframe_count"] == 1
        assert c["heading_count"] == 2

    def test_word_count(self):
        c = self._counts()
        # "alpha beta gamma" (3) + "cell" (1) + "Title" (1) + "Sub" (1)
        assert c["word_count"] == 6

    def test_external_link_count_never_negative(self):
        subtree = lxml_html.fromstring("<div><p>no links here</p></div>")
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        assert c["link_count"] == 0
        assert c["external_link_count"] == 0

    def test_anchor_to_different_page_is_not_an_anchor_link(self):
        subtree = lxml_html.fromstring(
            '<div><a href="https://example.com/other#frag">x</a></div>'
        )
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        # Resolves to a *different* path, so it is a normal link, not an anchor.
        assert c["anchor_link_count"] == 0
        assert c["link_count"] == 1


# Prose-rich main with links/images, surrounded by chrome carrying its own
# links and an image -- to prove counts are scoped to the located main region.
SCOPING_HTML = b"""
<!DOCTYPE html><html lang="en"><head><title>Scoping</title></head><body>
  <header><a href="/">home</a><a href="/about">about</a></header>
  <nav><a href="/news">news</a><img src="/logo.png"></nav>
  <main><article>
    <h1>Survey of the Coastal Pools</h1>
    <p>The rocky shelves along the northern coast hold dozens of tide pools that
       fill and drain with each turning of the sea, revealing anemones and crabs
       to anyone who arrives at the hour before the lowest tide. See the
       <a href="/methodology">methodology</a> for survey details.</p>
    <p>Naturalists have catalogued these pools for more than a century, returning
       season after season to record which species persist and which fade from
       the record. The full <a href="/archive">archive</a> spans decades of
       careful observation by volunteers along the entire shoreline.</p>
  </article></main>
  <footer><a href="/privacy">privacy</a><a href="/terms">terms</a>
          <img src="/seal.png"></footer>
</body></html>
"""


class TestCountScoping:
    def test_counts_exclude_chrome(self):
        result = extract_content(SCOPING_HTML)
        assert result.main_content_extracted is True
        c = count_structure(
            result.subtree,
            "https://example.com/survey",
            is_internal=_internal,
            asset_extensions=ASSET_EXTS,
        )
        # Only the two in-article links count; header/nav/footer links excluded.
        assert c["link_count"] == 2
        # No images live in the main region; nav/footer images excluded.
        assert c["image_count"] == 0
        assert c["heading_count"] == 1


class TestEmbedSignals:
    def test_benign_and_nonbenign_mix(self):
        subtree = lxml_html.fromstring(
            "<div>"
            '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
            '<iframe src="https://public.tableau.com/views/x"></iframe>'
            "</div>"
        )
        sig = embed_signals(subtree)
        assert sig["iframe_hosts"] == ["www.youtube.com", "public.tableau.com"]
        assert sig["embed_count_nonbenign"] == 1

    def test_no_iframes(self):
        subtree = lxml_html.fromstring("<div><p>nothing embedded</p></div>")
        sig = embed_signals(subtree)
        assert sig["iframe_hosts"] == []
        assert sig["embed_count_nonbenign"] == 0

    def test_distinct_hosts_but_per_element_nonbenign_count(self):
        subtree = lxml_html.fromstring(
            "<div>"
            '<iframe src="https://public.tableau.com/a"></iframe>'
            '<iframe src="https://public.tableau.com/b"></iframe>'
            "</div>"
        )
        sig = embed_signals(subtree)
        # One distinct host, but both elements are non-benign.
        assert sig["iframe_hosts"] == ["public.tableau.com"]
        assert sig["embed_count_nonbenign"] == 2

    def test_allowlist_override_reclassifies(self):
        subtree = lxml_html.fromstring(
            '<div><iframe src="https://public.tableau.com/a"></iframe></div>'
        )
        # With tableau.com allowlisted, the embed is benign.
        sig = embed_signals(subtree, benign_hosts=frozenset({"tableau.com"}))
        assert sig["embed_count_nonbenign"] == 0
        # Raw host signal is always present regardless of classification.
        assert sig["iframe_hosts"] == ["public.tableau.com"]

    def test_relative_iframe_ignored(self):
        subtree = lxml_html.fromstring(
            '<div><iframe src="/local/widget"></iframe></div>'
        )
        sig = embed_signals(subtree)
        assert sig["iframe_hosts"] == []
        assert sig["embed_count_nonbenign"] == 0


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello world") == content_hash("hello world")

    def test_hex_sha256_length(self):
        h = content_hash("some content")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_content_differs(self):
        assert content_hash("cat sat") != content_hash("cat ran")

    def test_empty_returns_empty_string(self):
        assert content_hash("") == ""

    def test_whitespace_change_is_insignificant_through_pipeline(self):
        a = content_hash(normalize_content_text("The quick   brown\nfox"))
        b = content_hash(normalize_content_text("The quick brown fox"))
        assert a == b

    def test_meaningful_change_through_pipeline_differs(self):
        a = content_hash(normalize_content_text("The quick brown fox"))
        b = content_hash(normalize_content_text("The quick red fox"))
        assert a != b

    def test_case_is_significant(self):
        assert content_hash("Apple") != content_hash("apple")

    def test_unicode_nfc_nfd_hash_equal(self):
        # "café" composed (NFC) vs decomposed (NFD) -> same hash after normalize.
        nfc = "café"
        nfd = "café"
        assert content_hash(normalize_content_text(nfc)) == content_hash(
            normalize_content_text(nfd)
        )


class TestMemoryHygiene:
    def test_repeated_calls_do_not_accumulate_state(self):
        # The module holds no per-page state; repeated extraction is stable.
        first = extract_content(ARTICLE_HTML)
        for _ in range(5):
            again = extract_content(ARTICLE_HTML)
            assert again.normalized_text == first.normalized_text
            assert again.main_content_extracted == first.main_content_extracted
