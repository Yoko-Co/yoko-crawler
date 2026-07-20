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
from embed_allowlist import load_benign_hosts
from website_spider import WebsiteSpider

BENIGN = load_benign_hosts()

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
        # A body over the size guard is skipped BEFORE any lxml parse (memory
        # safety): empty enrichment, no counts, empty hash input. Force the guard
        # with a low threshold rather than allocating a multi-MB payload.
        monkeypatch.setattr(ce, "MAX_BODY_BYTES", 64)
        result = extract_content(ARTICLE_HTML)
        assert len(ARTICLE_HTML) > ce.MAX_BODY_BYTES
        assert result.main_content_extracted is False
        # No parse happened, so there is no text to hash.
        assert result.normalized_text == ""


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

    def test_member_login_links_counted_and_excluded_from_content(self):
        # Inline member-login CTAs (corpus #61) are gates, not content: counted on their own and
        # kept out of link_count / internal_link_count / internal_link_targets.
        subtree = lxml_html.fromstring(
            "<div>"
            '<a href="/real-article">content</a>'                                  # real internal link
            '<a href="https://example.com?ReturnURL=%2Fx&do_oauth_login=abc">Log in</a>'  # oauth login
            '<a href="/account/login?redirect=/x">Sign in</a>'                     # login path
            '<a href="/wp-login.php?redirect_to=/y">WP login</a>'                  # wp-login path
            "</div>"
        )
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        assert c["member_login_link_count"] == 3
        # Only the one real internal link is counted as content; the three logins are excluded.
        assert c["link_count"] == 1
        assert c["internal_link_count"] == 1
        assert c["internal_link_targets"] == ["https://example.com/real-article"]

    def test_generic_redirect_params_are_not_login_ctas(self):
        # Precision guard (the #55-57 + PR #39 review lesson): generic "come back after this"
        # params are NOT login CTAs. A bare redirect_to=, and a donation/checkout return_url=
        # (dropped as a marker precisely because it rides non-login conversion links), must all
        # stay content.
        subtree = lxml_html.fromstring(
            '<div><a href="/blog/post">read</a>'
            '<a href="/search?redirect_to=/home">search</a>'
            '<a href="/donate?return_url=/thanks">donate</a>'
            '<a href="/x?partner_returnurl=1">partner</a></div>'
        )
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        assert c["member_login_link_count"] == 0
        assert c["internal_link_count"] == 4

    def test_login_segment_variants_hyphen_and_underscore(self):
        # /log-in (hyphen theme) and /users/sign_in (Rails/Devise underscore) are real logins.
        subtree = lxml_html.fromstring(
            '<div><a href="/log-in">a</a><a href="/users/sign_in">b</a></div>'
        )
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        assert c["member_login_link_count"] == 2
        assert c["internal_link_count"] == 0

    def test_login_path_segment_matches_exactly_not_as_substring(self):
        # PR #39 review (P2): a path that merely STARTS with a login marker is content, not a
        # login CTA. /login-help, /signing-bonus, /sign-in-sheet, /bloginfo must all stay content;
        # /account/login and /en/login (login as a whole segment) are real logins.
        subtree = lxml_html.fromstring(
            "<div>"
            '<a href="/login-help">a</a>'      # starts with 'login' but different segment
            '<a href="/signing-bonus">b</a>'   # starts with 'signin' but different segment
            '<a href="/sign-in-sheet">c</a>'   # starts with 'sign-in' but different segment
            '<a href="/bloginfo">d</a>'        # 'login' mid-segment
            '<a href="/account/login">e</a>'   # real: login segment
            '<a href="/en/login">f</a>'        # real: login segment under a locale prefix
            "</div>"
        )
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        assert c["member_login_link_count"] == 2  # only the two real logins
        assert c["internal_link_count"] == 4      # the four content links survive

    def test_login_links_keep_the_external_count_balanced(self):
        # Balance invariant with a mix of external + login links present (review testing-gap).
        subtree = lxml_html.fromstring(
            "<div>"
            '<a href="/real">a</a>'                                  # internal content
            '<a href="https://other.org/x">b</a>'                    # external content
            '<a href="https://example.com/login">c</a>'              # internal login (excluded)
            "</div>"
        )
        c = count_structure(
            subtree, PAGE_URL, is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        assert c["member_login_link_count"] == 1
        assert c["link_count"] == 2 and c["internal_link_count"] == 1
        assert c["external_link_count"] == c["link_count"] - c["internal_link_count"] == 1

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
        sig = embed_signals(subtree, BENIGN)
        assert sig["iframe_hosts"] == ["www.youtube.com", "public.tableau.com"]
        assert sig["embed_count_nonbenign"] == 1

    def test_no_iframes(self):
        subtree = lxml_html.fromstring("<div><p>nothing embedded</p></div>")
        sig = embed_signals(subtree, BENIGN)
        assert sig["iframe_hosts"] == []
        assert sig["embed_count_nonbenign"] == 0

    def test_distinct_hosts_but_per_element_nonbenign_count(self):
        subtree = lxml_html.fromstring(
            "<div>"
            '<iframe src="https://public.tableau.com/a"></iframe>'
            '<iframe src="https://public.tableau.com/b"></iframe>'
            "</div>"
        )
        sig = embed_signals(subtree, BENIGN)
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
        sig = embed_signals(subtree, BENIGN)
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


class TestHashCanary:
    def test_hash_canary_is_stable(self):
        # A known input maps to a fixed digest. If this changes, the
        # normalization pipeline or hash drifted (a deliberate hash-epoch change).
        assert content_hash(normalize_content_text("The quick brown fox")) == (
            "5cac4f980fedc3d3f1f99b4be3472c9b30d56523e632d151237ec9309048bda9"
        )


class TestExtractContentLocation:
    def test_role_main_div_is_located(self):
        # A div[role=main] with no <main>/<article> still scopes correctly.
        html = (
            b"<html><body>"
            b"<nav><a href='/'>NAVHOME</a></nav>"
            b"<div role='main'><h1>Coastal Survey Report</h1>"
            b"<p>The rocky shelves along the northern coast hold dozens of tide "
            b"pools that fill and drain with each turning of the sea, revealing "
            b"anemones and crabs to anyone arriving before the lowest tide.</p>"
            b"<p>Naturalists have catalogued these pools for over a century, "
            b"season after season, recording which species persist and fade.</p>"
            b"</div>"
            b"<footer>FOOTERCOPYRIGHT</footer></body></html>"
        )
        result = extract_content(html)
        assert result.main_content_extracted is True
        assert "NAVHOME" not in result.subtree.text_content()
        assert "FOOTERCOPYRIGHT" not in result.subtree.text_content()

    def test_text_extracted_but_no_container_clears_threshold(self, monkeypatch):
        # trafilatura returns real text, but force location to find no qualifying
        # container: counts fall back to the DE-CHROMED <body> (issue #9), but the (better)
        # main text is still hashed (normalized_text non-empty).
        monkeypatch.setattr(ce, "_locate_main_subtree", lambda body_el, main: None)
        result = extract_content(ARTICLE_HTML)
        assert result.main_content_extracted is False
        assert result.normalized_text  # main text retained for hashing
        # subtree is now a de-chromed COPY of the body (not the body itself), so chrome is
        # excluded from counts while the full body stays intact for page-wide signals.
        assert result.subtree is not result.body_subtree
        assert "NAVHOME" not in result.subtree.text_content()
        assert "FOOTERCOPYRIGHT" not in result.subtree.text_content()
        assert "FOOTERCOPYRIGHT" in result.body_subtree.text_content()

    def test_div_soup_over_cap_falls_back_to_body(self, monkeypatch):
        # When generic candidates exceed the cap, locating gives up (body-scope)
        # rather than doing O(n*m) work. Force a tiny cap.
        monkeypatch.setattr(ce, "_MAX_LOCATE_CANDIDATES", 1)
        html = (
            b"<html><body>"
            b"<div><p>The rocky shelves along the northern coast hold dozens of "
            b"tide pools that fill and drain with each turning of the sea.</p></div>"
            b"<div><p>Naturalists have catalogued these pools for over a century, "
            b"season after season, recording which species persist and fade.</p></div>"
            b"</body></html>"
        )
        result = extract_content(html)
        assert result.main_content_extracted is False

    def test_trafilatura_exception_falls_back_to_body(self, monkeypatch):
        # If trafilatura raises on pathological markup, extraction is treated as
        # failure: body-scoped counts, body text hashed.
        def _boom(*a, **k):
            raise RuntimeError("trafilatura blew up")

        monkeypatch.setattr(ce, "bare_extraction", _boom)
        result = extract_content(ARTICLE_HTML)
        assert result.main_content_extracted is False
        assert result.normalized_text  # body text, non-empty

    def test_fragment_without_body_is_countable(self):
        result = extract_content(b"<div><p>just a fragment with some text</p></div>")
        assert result.subtree is not None
        assert result.body_subtree is not None

    def test_embeds_scoped_page_wide_not_main(self):
        # An iframe in the footer (outside main) is captured by body_subtree,
        # which is what _enrichment uses for embed signals.
        html = (
            b"<html><body>"
            b"<main><article><h1>Survey of the Coastal Pools</h1>"
            b"<p>The rocky shelves along the northern coast hold dozens of tide "
            b"pools that fill and drain with each turning of the sea, revealing "
            b"anemones and crabs to anyone arriving before the lowest tide.</p>"
            b"<p>Naturalists have catalogued these pools for over a century, "
            b"season after season, recording which species persist and fade.</p>"
            b"</article></main>"
            b"<footer><iframe src='https://public.tableau.com/views/x'></iframe>"
            b"</footer></body></html>"
        )
        result = extract_content(html)
        assert result.main_content_extracted is True
        # Main subtree excludes the footer iframe...
        assert embed_signals(result.subtree, BENIGN)["embed_count_nonbenign"] == 0
        # ...but the page-wide body_subtree catches it.
        body_sig = embed_signals(result.body_subtree, BENIGN)
        assert body_sig["embed_count_nonbenign"] == 1
        assert "public.tableau.com" in body_sig["iframe_hosts"]


class TestMemoryHygiene:
    def test_repeated_calls_do_not_accumulate_state(self):
        # The module holds no per-page state; repeated extraction is stable.
        first = extract_content(ARTICLE_HTML)
        for _ in range(5):
            again = extract_content(ARTICLE_HTML)
            assert again.normalized_text == first.normalized_text
            assert again.main_content_extracted == first.main_content_extracted


# Fallback page (no <main>/<article>, thin content) wrapped in heavy chrome: nav, a
# banner header carrying the per-page search box, an aside, and a footer with a tracking
# iframe. The real content is a short sentence with two links + a subscribe form.
_FALLBACK_CHROME_HTML = b"""
<!DOCTYPE html><html><head><title>T</title></head>
<body>
  <nav><a href="/n1">N1</a><a href="/n2">N2</a><a href="/n3">N3</a>
       <a href="/n4">N4</a><a href="/n5">N5</a></nav>
  <header role="banner">
    <a href="/logo">LOGO</a>
    <form role="search" action="/"><input name="s"></form>
  </header>
  <div class="entry">
    <p>Read <a href="/one">one</a> and <a href="/two">two</a>.</p>
    <form action="/subscribe"><input name="email"></form>
  </div>
  <aside><a href="/side">SIDE</a></aside>
  <footer>
    <a href="/f1">F1</a><a href="/f2">F2</a>
    <iframe src="https://tracker.evil/x"></iframe>
  </footer>
</body></html>
"""


class TestChromeAwareCounting:
    """issue #9: on the body-fallback path, structural counts exclude site chrome (nav /
    banner header / aside / footer + the per-page search box) so they measure content, not
    the theme -- while page-wide embed detection still sees chrome (a footer tracking
    iframe)."""

    def _counts(self, subtree):
        return count_structure(subtree, "https://example.com/p", is_internal=_internal, asset_extensions=ASSET_EXTS)

    def test_falls_back_and_strips_chrome_from_counts(self):
        result = extract_content(_FALLBACK_CHROME_HTML)
        assert result.main_content_extracted is False  # thin content -> body fallback
        sub = self._counts(result.subtree)
        # Only the content div survives: its 2 links + 1 real form. Nav/header/aside/footer
        # links and the header search form are gone.
        assert sub["link_count"] == 2
        assert sub["form_count"] == 1

    def test_body_subtree_still_carries_chrome(self):
        # The full body (used for page-wide signals) is untouched -- proving the counts
        # difference is real de-chroming, not a parse quirk.
        result = extract_content(_FALLBACK_CHROME_HTML)
        body = self._counts(result.body_subtree)
        assert body["link_count"] > 2  # nav/footer/aside/logo links present in the body
        assert body["form_count"] == 2  # search box + subscribe form

    def test_footer_embed_still_detected_page_wide(self):
        # The de-chroming must NOT hide a surprising embed: embed_signals runs over the full
        # body, so the footer tracking iframe is still flagged.
        result = extract_content(_FALLBACK_CHROME_HTML)
        signals = embed_signals(result.body_subtree, BENIGN)
        assert signals["embed_count_nonbenign"] == 1

    def test_article_nested_header_footer_are_kept(self):
        # An article's own <header>/<footer> (title/H1, byline) is content, not chrome.
        body = lxml_html.fromstring(
            b"<body><header><a href='/nav'>NAV</a></header>"
            b"<article><header><h1><a href='/self'>Title</a></h1></header>"
            b"<p>body</p><footer><a href='/byline'>By Someone</a></footer></article>"
            b"<footer><a href='/site'>SITE</a></footer></body>"
        )
        dechromed = ce._dechrome(body)
        hrefs = {a.get("href") for a in dechromed.xpath(".//a[@href]")}
        assert "/self" in hrefs and "/byline" in hrefs  # article header/footer kept
        assert "/nav" not in hrefs and "/site" not in hrefs  # site header/footer dropped

    def test_svg_text_not_counted_as_words(self):
        # <svg> is non-content: its inline text must not inflate the word count.
        result = extract_content(
            b"<html><body><div><p>Real words here plainly.</p>"
            b"<svg><text>ICONWORDONE ICONWORDTWO ICONWORDTHREE</text></svg></div></body></html>"
        )
        # svg dropped in _parse_body, so its text is absent from both subtree and body.
        assert "ICONWORD" not in result.subtree.text_content()

    def test_content_misplaced_in_a_chrome_tag_is_not_zeroed(self):
        # P1 guard: a theme that wraps REAL content in <nav> must NOT be de-chromed to empty
        # (false-simple under-scopes a real project -- worse than over-counting).
        html = (
            b"<html><body><nav><h1>My Great Article Title</h1>"
            b"<p>The northern coastline holds dozens of tide pools that fill and drain with "
            b"each turning of the sea, revealing anemones and hermit crabs to visitors.</p>"
            b"</nav></body></html>"
        )
        result = extract_content(html)
        assert result.main_content_extracted is False  # trafilatura calls nav boilerplate
        # The nav holds real prose, so it's kept -- the page is not zeroed.
        assert "tide pools" in result.subtree.text_content()
        assert self._counts(result.subtree)["word_count"] > 20

    def test_role_wrapper_around_an_article_is_kept(self):
        # P1 guard: a misused role=contentinfo wrapping a real <article> must not delete it.
        html = (
            b"<html><body><div role='contentinfo'><article><h1>Report</h1>"
            b"<p>Substantial article content that should survive de-chroming entirely.</p>"
            b"</article></div></body></html>"
        )
        result = extract_content(html)
        assert "Substantial article content" in result.subtree.text_content()

    def test_body_wrapping_main_still_drops_site_header_and_footer(self):
        # P2 fix: a page builder wraps the whole body in one <main>; <main> is NOT a content
        # signal, so the link-dense site header/footer inside it are still stripped.
        html = (
            b"<html><body><main>"
            b"<header><a href='/h1'>H1</a><a href='/h2'>H2</a><a href='/h3'>H3</a></header>"
            b"<div class='c'><p>Read <a href='/one'>one</a>.</p></div>"
            b"<footer><a href='/f1'>F1</a><a href='/f2'>F2</a></footer>"
            b"</main></body></html>"
        )
        result = extract_content(html)
        assert result.main_content_extracted is False
        assert self._counts(result.subtree)["link_count"] == 1  # only the content link

    def test_in_article_nav_is_kept(self):
        # An article's own table-of-contents <nav> is content, not chrome.
        body = lxml_html.fromstring(
            b"<body><nav><a href='/site'>SITE</a></nav>"
            b"<article><nav><a href='#s1'>Section 1</a></nav><p>body</p></article></body>"
        )
        dechromed = ce._dechrome(body)
        hrefs = {a.get("href") for a in dechromed.xpath(".//a[@href]")}
        assert "#s1" in hrefs  # in-article TOC nav kept
        assert "/site" not in hrefs  # site nav dropped

    def test_all_chrome_page_counts_zero_but_hash_is_non_empty(self):
        # A menu/sitemap page (only nav + footer) de-chromes to empty counts, but the hash
        # still reflects the full body text (change detection unaffected). Documents the
        # intentional word_count=0 / content_hash!="" state for a chrome-only HTML page --
        # distinct from empty_enrichment (hash="") which means a non-HTML/no-body row.
        html = b"<html><body><nav><a href='/a'>A</a><a href='/b'>B</a></nav><footer>Copyright</footer></body></html>"
        result = extract_content(html)
        assert self._counts(result.subtree)["link_count"] == 0
        assert result.normalized_text != ""  # hash input non-empty

    def test_div_soup_nav_stripped_by_classname(self):
        # issue #53: non-semantic chrome built from plain <div>s (no nav tag / role) is now
        # stripped by class-name token, so a theme like ndba.com's div.mobileNavDiv no longer
        # leaks its whole site menu into the content link count.
        body = lxml_html.fromstring(
            b"<body><div class='menu'><a href='/a'>A</a><a href='/b'>B</a></div><p>x</p></body>"
        )
        hrefs = {a.get("href") for a in ce._dechrome(body).xpath(".//a[@href]")}
        assert "/a" not in hrefs and "/b" not in hrefs  # div-soup nav stripped

    def test_camelcase_nav_div_stripped(self):
        # The real ndba.com shape: a camelCase class 'mobileNavDiv' -> {mobile,nav,div} hits 'nav'.
        body = lxml_html.fromstring(
            b"<body><div class='mobileNavDiv'><a href='/x'>Advocacy</a><a href='/y'>About</a>"
            b"</div><p>real page text</p></body>"
        )
        hrefs = {a.get("href") for a in ce._dechrome(body).xpath(".//a[@href]")}
        assert "/x" not in hrefs and "/y" not in hrefs

    def test_content_div_with_navish_class_but_prose_is_kept(self):
        # The content guard still wins: a block whose class matches a token but which holds real
        # prose (>= _MIN_CHROME_PROSE_WORDS) is NOT stripped -- widening chrome NAMES never
        # widens the removal conditions.
        prose = b" ".join([b"word"] * 40)
        body = lxml_html.fromstring(
            b"<body><div class='menu-section'><a href='/keep'>k</a><p>" + prose + b"</p></div></body>"
        )
        hrefs = {a.get("href") for a in ce._dechrome(body).xpath(".//a[@href]")}
        assert "/keep" in hrefs  # prose-bearing block survives despite the 'menu' token

    def test_masthead_hero_with_title_image_kept_on_fallback(self):
        # issue #53 review: a name-matched hero (masthead/menu class) holding the page H1 + hero
        # image must NOT be stripped on the body-fallback path, even with <25 prose words -- and
        # both strip paths must agree (no asymmetry).
        body = lxml_html.fromstring(
            b"<body><div class='masthead'><h1>Our Story</h1><img src='/hero.jpg'>"
            b"<a href='/read'>Read more</a></div></body>"
        )
        assert ce._dechrome(body).xpath("count(.//h1)") == 1
        assert ce._dechrome(body).xpath("count(.//img)") == 1
        assert ce._dechrome_menus(body).xpath("count(.//h1)") == 1  # parity

    def test_menu_classed_gallery_images_kept_on_fallback(self):
        # A 'menu-gallery' of image tiles is content (images), not a nav menu -> images survive.
        tiles = b"".join(b"<a href='/p%d'><img src='/i%d.jpg'></a>" % (i, i) for i in range(6))
        body = lxml_html.fromstring(b"<body><div class='menu-gallery'>" + tiles + b"</div></body>")
        assert ce._dechrome(body).xpath("count(.//img)") == 6

    def test_pure_navlist_no_heading_or_image_still_stripped(self):
        # The ndba shape: a nav-classed div of link items with NO heading/image/prose is chrome.
        body = lxml_html.fromstring(
            b"<body><div class='mobileNavDiv'><ul><li><a href='/a'>A</a></li>"
            b"<li><a href='/b'>B</a></li></ul></div><p>x</p></body>"
        )
        assert "/a" not in {a.get("href") for a in ce._dechrome(body).xpath(".//a[@href]")}

    def test_link_list_page_in_a_nav_is_not_zeroed_on_fallback(self):
        # issue #54 review (P0 guard): a genuine HTML sitemap / A-Z index whose CONTENT is a link
        # list wrapped in <nav> WITH a heading must NOT be zeroed on the body-fallback path -- the
        # heading-guard in _holds_content keeps it (the reverted #54 change no longer rejects it for
        # being link-dominated). Properly nested via _doc so the <nav> is NOT the drop-immune root.
        links = b"".join(b"<li><a href='/term-%d'>Glossary Term %d</a></li>" % (i, i) for i in range(30))
        result = extract_content(_doc(b"<nav role='navigation'><h1>A to Z Site Directory</h1><ul>" + links + b"</ul></nav>"))
        c = self._counts(result.subtree)
        assert c["internal_link_count"] == 30 and c["word_count"] > 0  # headed directory survives

    def test_is_link_dominated_menu_discriminator(self):
        # The link-domination tell used by the site-frame strip: > floor links AND more link-text
        # than prose. A menu is dominated; a prose colophon and an image gallery are not; and a
        # text-bearing named anchor (no href) counts as PROSE, not link-words (traversal parity).
        menu = lxml_html.fromstring(
            b"<footer>" + b"".join(b"<a href='/s%d'>Section Number %d</a>" % (i, i) for i in range(8)) + b"</footer>"
        )
        assert ce._is_link_dominated_menu(menu) is True
        colophon = lxml_html.fromstring(
            b"<footer><p>" + b" ".join([b"word"] * 40) + b" see <a href='/src'>source</a>.</p></footer>"
        )
        assert ce._is_link_dominated_menu(colophon) is False  # prose outweighs the lone link
        gallery = lxml_html.fromstring(
            b"<footer>" + b"".join(b"<a href='/g%d'><img src='/i%d.jpg'></a>" % (i, i) for i in range(8)) + b"</footer>"
        )
        assert ce._is_link_dominated_menu(gallery) is False  # links wrap images: ~0 link-words
        named = lxml_html.fromstring(
            b"<footer><a name='top'>Jump To The Very Top Of This Long Page Section Here</a>"
            b"<a href='/1'>A</a><a href='/2'>B</a><a href='/3'>C</a><a href='/4'>D</a></footer>"
        )
        assert ce._is_link_dominated_menu(named) is False  # named-anchor text is prose, not a label

    def test_name_tokens_precision(self):
        # 'nav'/'menu' as whole tokens hit; substrings inside real words do not.
        assert ce._has_chrome_name(lxml_html.fromstring(b"<div class='site-nav'></div>"))
        assert ce._has_chrome_name(lxml_html.fromstring(b"<div id='mainMenu'></div>"))
        assert not ce._has_chrome_name(lxml_html.fromstring(b"<div class='canvas'></div>"))
        assert not ce._has_chrome_name(lxml_html.fromstring(b"<div class='navel-gazing'></div>"))
        assert not ce._has_chrome_name(lxml_html.fromstring(b"<div class='main-content'></div>"))


def _doc(inner: bytes) -> bytes:
    """Wrap fixture markup in a well-formed document (lxml mis-parses a bare <body>
    fragment, dropping <main>/<div>, which would defeat main-region location)."""
    return b"<!DOCTYPE html><html lang=en><head><title>T</title></head><body>" + inner + b"</body></html>"


# ~55 words of real prose -- enough to clear the _MIN_REGION_PROSE_WORDS gate, so a region
# holding it is trusted verbatim (its own in-content nav/footer is counted, not stripped).
_PROSE = (
    b"The rocky shelves along the northern coast hold dozens of tide pools that fill and drain "
    b"with each turning of the sea revealing anemones and crabs to anyone who arrives at the hour "
    b"before the lowest tide and stays to watch the water recede across the flats. Naturalists "
    b"have catalogued these pools for over a century, returning season after season to record them."
)

# A genuine content page (<main>, substantial prose) with its OWN in-content pagination nav and
# a related-links footer. The reviewers' regression case: pre-#13-fix these link-dense but
# legitimate in-content navs were counted; the fix must NOT strip them (region has real prose).
_CONTENT_WITH_INPAGE_NAV = _doc(
    b"<main class='entry-content'><h1>Chapter Seven</h1><p>" + _PROSE + b"</p>"
    b"<nav class='pagination'><a href='/p1'>1</a><a href='/p2'>2</a><a href='/p3'>3</a>"
    b"<a href='/p4'>4</a><a href='/p5'>5</a><a href='/p6'>6</a></nav>"
    b"<footer class='related'><a href='/r1'>One</a><a href='/r2'>Two</a></footer></main>"
)

# A table-of-contents nav + entry footer inside a region that is a DESCENDANT of <article>
# (the article carries a byline outside the nested role=main, so locate settles on role=main).
# The adversarial finding: a deepcopy would sever the <article> ancestor and strip these. With
# the prose gate this region is trusted verbatim, so ancestry is never consulted.
_ARTICLE_DESCENDANT_TOC = _doc(
    b"<article><header class='byline'>By Jane Doe, senior correspondent, filed Tuesday</header>"
    b"<div role='main'><h1>Report Title</h1>"
    b"<nav class='toc'><a href='#a'>A</a><a href='#b'>B</a><a href='#c'>C</a></nav>"
    b"<p>" + _PROSE + b"</p>"
    b"<footer class='entry-footer'><a href='/r1'>One</a><a href='/r2'>Two</a></footer></div></article>"
)

# A THIN page (prose < the region floor) whose title + hero image live in a <header>: the
# prose-thin branch must keep this link-sparse header (title/hero is content, not chrome).
_THIN_TITLE_HERO = _doc(
    b"<main><header class='eh'><h1>Current Sponsors</h1><img src='/hero.png'></header>"
    b"<p>Thank you to our sponsors this year.</p></main>"
)


class TestSuccessPathProseGate:
    """issue #13: on the SUCCESS path a located region with real prose of its own is trusted
    verbatim -- its in-content nav/footer/header is content, not chrome -- while only a
    PROSE-THIN region (trafilatura latched onto the nav) has its leaked site menus stripped.
    Guards the two reviewer findings: genuine content is never under-counted, and de-chroming
    a real region (which a deepcopy would sever from its <article> ancestor) never happens."""

    def _counts(self, subtree):
        return count_structure(subtree, "https://example.com/p", is_internal=_internal, asset_extensions=ASSET_EXTS)

    def test_content_region_with_inpage_nav_is_kept_verbatim(self):
        # Real prose -> verbatim: the 6 pagination links + 2 related links are ALL counted.
        result = extract_content(_CONTENT_WITH_INPAGE_NAV)
        assert result.main_content_extracted is True
        assert self._counts(result.subtree)["link_count"] == 8

    def test_toc_inside_article_descendant_is_kept(self):
        # The severed-ancestry case: TOC anchors + entry-footer links survive (region has prose).
        result = extract_content(_ARTICLE_DESCENDANT_TOC)
        hrefs = {a.get("href") for a in result.subtree.xpath(".//a[@href]")}
        assert {"#a", "#b", "#c", "/r1", "/r2"} <= hrefs

    def test_thin_region_keeps_link_sparse_title_and_hero(self):
        # Prose-thin branch: the title heading and hero image (a link-sparse <header>) are kept.
        result = extract_content(_THIN_TITLE_HERO)
        c = self._counts(result.subtree)
        assert c["heading_count"] == 1 and c["image_count"] == 1

    def test_thin_navlatch_page_does_not_count_site_nav(self):
        # A thin page dominated by a site nav + footer must not report them as page content:
        # whichever prose-thin path handles it, only the single in-content link survives.
        html = _doc(
            b"<div class='site-inner'>"
            b"<nav>" + b"".join(b"<a href='/nav%d'>Menu %d</a>" % (i, i) for i in range(12)) + b"</nav>"
            b"<main><h1>Current Sponsors</h1><p>Thank you to <a href='/big'>Big Co</a>.</p></main>"
            b"<footer role='contentinfo'><a href='/privacy'>Privacy</a><a href='/terms'>Terms</a>"
            b"<a href='/map'>Sitemap</a><a href='/x'>X</a></footer></div>"
        )
        result = extract_content(html)
        assert {a.get("href") for a in result.subtree.xpath(".//a[@href]")} == {"/big"}


class TestDechromeMenus:
    """issue #13: _dechrome_menus strips only LINK-DENSE chrome from a prose-thin region and is
    ancestry-free (immune to the deepcopy that severs an <article> parent)."""

    def test_strips_dense_menu_keeps_sparse_header_and_footer(self):
        el = lxml_html.fromstring(
            b"<div><nav><a href='/1'>1</a><a href='/2'>2</a><a href='/3'>3</a>"
            b"<a href='/4'>4</a><a href='/5'>5</a></nav>"
            b"<header><h1>Title</h1><img src='/hero.png'></header>"
            b"<footer><a href='/only'>only</a></footer></div>"
        )
        out = ce._dechrome_menus(el)
        hrefs = {a.get("href") for a in out.xpath(".//a[@href]")}
        assert hrefs == {"/only"}  # 5-link nav dropped; 1-link footer kept
        assert out.xpath("count(.//h1)") == 1 and out.xpath("count(.//img)") == 1  # header kept


class TestDechromeSiteFrame:
    """issue #54: on the trusted (prose-rich) path, locate can settle on a wide wrapper that also
    holds the SITE frame. Only a link-DOMINATED site <header>/<footer> is stripped; in-content
    <nav> (pagination), link-sparse footers, in-article footers, and galleries are kept."""

    def test_strips_link_dominated_frame_keeps_content_pagination_and_sparse_footer(self):
        # The openprimaries shape: a wrapper holding a site header (link menu), a <main> with prose
        # + in-content pagination + a link-sparse related footer, and a site footer (link menu).
        el = lxml_html.fromstring(
            b"<div class='wrap'>"
            b"<header class='site'>" + b"".join(b"<a href='/h%d'>Header Link %d</a>" % (i, i) for i in range(8)) + b"</header>"
            b"<main><h1>Title</h1><p>Real prose content of the page goes here.</p>"
            b"<nav class='pagination'><a href='/p1'>1</a><a href='/p2'>2</a><a href='/p3'>3</a><a href='/p4'>4</a></nav>"
            b"<footer class='related'><a href='/r1'>One</a><a href='/r2'>Two</a></footer></main>"
            b"<footer class='site'><h4>ABOUT</h4>" + b"".join(b"<a href='/f%d'>Footer Link %d</a>" % (i, i) for i in range(6)) + b"</footer>"
            b"</div>"
        )
        hrefs = {a.get("href") for a in ce._dechrome_site_frame(el).xpath(".//a[@href]")}
        assert not any(h.startswith("/h") or h.startswith("/f") for h in hrefs)  # site frame stripped
        assert {"/p1", "/p2", "/p3", "/p4", "/r1", "/r2"} <= hrefs  # pagination + sparse footer kept

    def test_in_article_footer_is_kept(self):
        # An article's OWN footer (within content) is not the site frame, even if link-dominated.
        el = lxml_html.fromstring(
            b"<div><article><p>Body.</p><footer>"
            + b"".join(b"<a href='/t%d'>Tag Number %d</a>" % (i, i) for i in range(6))
            + b"</footer></article></div>"
        )
        assert any(h.startswith("/t") for h in {a.get("href") for a in ce._dechrome_site_frame(el).xpath(".//a[@href]")})

    def test_image_gallery_footer_is_kept(self):
        # A footer of image tiles: links wrap images (link-word-sparse), so NOT link-dominated.
        tiles = b"".join(b"<a href='/g%d'><img src='/i%d.jpg'></a>" % (i, i) for i in range(8))
        el = lxml_html.fromstring(b"<div><footer class='gallery'>" + tiles + b"</footer></div>")
        hrefs = {a.get("href") for a in ce._dechrome_site_frame(el).xpath(".//a[@href]")}
        assert len([h for h in hrefs if h.startswith("/g")]) == 8

    def test_div_soup_page_with_no_container_is_left_verbatim(self):
        # issue #54 review (P2 guard): a div-soup page (Elementor/Divi/older WP -- no <main>/
        # <article> anywhere) whose title + hero + taxonomy byline live in a link-dominated
        # <header class='entry-header'> must NOT be stripped. With no content container we can't
        # tell the page's own header from the site frame, so strip nothing (over-count is safe).
        region = lxml_html.fromstring(
            b"<div class='post'><header class='entry-header'><h1>The Coastal Survey Results</h1>"
            b"<p>by <a href='/author/jane'>Jane Doe</a> in <a href='/cat/news'>News</a> "
            + b" ".join(b"<a href='/tag/t%d'>topic%d</a>" % (i, i) for i in range(6)) + b"</p>"
            b"<img src='/feat.jpg'></header>"
            b"<div class='content'><p>Forty five words of real article prose go right here.</p></div></div>"
        )
        out = ce._dechrome_site_frame(region)
        assert out.xpath("count(.//h1)") == 1 and out.xpath("count(.//img)") == 1  # title + hero kept
        assert "/author/jane" in {a.get("href") for a in out.xpath(".//a[@href]")}

    def test_main_based_entry_header_with_title_hero_and_byline_is_kept(self):
        # issue #54 review (P1 guard): a <main>-based post whose in-content <header> carries the
        # title (h1) + hero image + a link-dominated byline (author/category/tags) must NOT be
        # stripped -- it is the content container's OWN header, not the site frame.
        region = lxml_html.fromstring(
            b"<main><header class='entry-header'><h1>Brief</h1>"
            b"<p>by <a href='/author/jane'>Jane Doe</a> in <a href='/cat/news'>News</a> "
            + b" ".join(b"<a href='/tag/t%d'>topic%d</a>" % (i, i) for i in range(6)) + b"</p>"
            b"<img src='/feat.jpg' alt='featured'></header>"
            b"<div class='content'><p>Forty five words of real article prose go right here.</p></div></main>"
        )
        out = ce._dechrome_site_frame(region)
        assert out.xpath("count(.//h1)") == 1 and out.xpath("count(.//img)") == 1  # title + hero kept
        assert {"/author/jane", "/cat/news", "/tag/t0"} <= {a.get("href") for a in out.xpath(".//a[@href]")}

    def test_region_inside_article_is_left_verbatim(self):
        # Ancestry guard (#54 review): when the located region is itself inside an <article>, the
        # whole region is article content -- even a link-dominated <footer> in it is the article's
        # own, and the deepcopy would sever the <article> ancestor -- so strip nothing.
        article = lxml_html.fromstring(
            b"<article><div role='main'><h1>Story</h1><p>Body prose here.</p>"
            b"<footer>" + b"".join(b"<a href='/t%d'>Tag Number %d</a>" % (i, i) for i in range(6))
            + b"</footer></div></article>"
        )
        region = article.xpath(".//div[@role='main']")[0]
        hrefs = {a.get("href") for a in ce._dechrome_site_frame(region).xpath(".//a[@href]")}
        assert len([h for h in hrefs if h.startswith("/t")]) == 6  # in-article footer kept verbatim

    def test_leaves_in_content_nav_untouched(self):
        # A link-dominated in-content <nav> (not header/footer) is NOT site frame -> kept verbatim,
        # preserving the issue-#13 "trust the region's own navigation" guarantee.
        el = lxml_html.fromstring(
            b"<div><main><p>Prose.</p><nav>"
            + b"".join(b"<a href='/n%d'>Nav Item %d</a>" % (i, i) for i in range(8))
            + b"</nav></main></div>"
        )
        assert len({a.get("href") for a in ce._dechrome_site_frame(el).xpath(".//a[@href]")}) == 8

    def test_operates_on_a_copy(self):
        el = lxml_html.fromstring(
            b"<div><nav><a href='/1'>1</a><a href='/2'>2</a><a href='/3'>3</a><a href='/4'>4</a></nav><p>x</p></div>"
        )
        ce._dechrome_menus(el)
        assert el.xpath("count(.//nav)") == 1  # original untouched

    def test_nav_nested_in_hero_header_drops_only_the_nav(self):
        # Standard markup: the site nav lives INSIDE the hero <header> alongside the title and
        # logo. The header is a container (has an <h1>/<img>), so it must be kept and only its
        # nested <nav> dropped -- not the whole header (which would zero the title + hero image).
        el = lxml_html.fromstring(
            b"<div><header class='hero'><img src='/logo.png'><h1>Our 2026 Sponsors</h1>"
            b"<nav><a href='/home'>Home</a><a href='/about'>About</a>"
            b"<a href='/sponsors'>Sponsors</a><a href='/contact'>Contact</a></nav></header></div>"
        )
        out = ce._dechrome_menus(el)
        assert out.xpath("count(.//h1)") == 1 and out.xpath("count(.//img)") == 1  # title+hero kept
        assert out.xpath("count(.//a[@href])") == 0  # the 4-link nav inside the header is dropped


class TestComponentSignals:
    """issue #12: detect interactive JS components (sliders/carousels/accordions/tabs/
    galleries) by container markers -- real dev work otherwise invisible or miscounted."""

    def _n(self, html):
        return ce.component_signals(lxml_html.fromstring(html))["component_count"]

    def test_slider_container_counts_once_not_its_slides(self):
        assert self._n(
            '<body><div class="swiper"><div class="swiper-slide">a</div>'
            '<div class="swiper-slide">b</div></div></body>'
        ) == 1

    def test_multiple_distinct_components(self):
        assert self._n(
            '<body><div class="carousel"><div class="carousel-item">x</div></div>'
            '<div class="accordion">y</div></body>'
        ) == 2

    def test_attribute_and_role_markers(self):
        assert self._n('<body><section data-slider="true">s</section></body>') == 1
        assert self._n('<body><ul role="tablist"><li>t</li></ul></body>') == 1

    def test_no_components(self):
        assert self._n('<body><p>text</p><table class="data-table"><tr><td>x</td></tr></table></body>') == 0

    def test_orphan_child_token_not_matched(self):
        # a stray swiper-slide with no container must not count.
        assert self._n('<body><div class="swiper-slide">orphan</div></body>') == 0

    def test_multi_class_container_counts_once(self):
        assert self._n('<body><div class="owl-carousel owl-theme">c</div></body>') == 1

    def test_bootstrap_tab_widget_counts_once_not_per_button(self):
        # nav-tabs + role=tablist on the container -> 1; the per-button data-bs-toggle
        # triggers must NOT each add a component (the item-level over-count fix).
        assert self._n(
            '<body><ul class="nav nav-tabs" role="tablist">'
            '<button data-bs-toggle="tab">A</button>'
            '<button data-bs-toggle="tab">B</button>'
            '<button data-bs-toggle="tab">C</button></ul></body>'
        ) == 1

    def test_fancybox_per_link_gallery_not_over_counted(self):
        # A bare per-link Fancybox gallery (no container class) is missed, not counted 3x.
        assert self._n(
            '<body><a data-fancybox="g" href="1.jpg">1</a>'
            '<a data-fancybox="g" href="2.jpg">2</a>'
            '<a data-fancybox="g" href="3.jpg">3</a></body>'
        ) == 0

    def test_nested_components_both_count(self):
        # a slider inside an accordion -> 2 distinct components.
        assert self._n(
            '<body><div class="accordion"><div class="swiper">'
            '<div class="swiper-slide">a</div></div></div></body>'
        ) == 2


class TestSliderSignals:
    """issue #25: slider_count is the carousel/slider SUBSET of component_count -- an image
    slider is interactive dev work, kept distinct from a wall of standalone images."""

    def _n(self, html):
        return ce.slider_signals(lxml_html.fromstring(html))["slider_count"]

    def test_slider_container_counts_once_not_its_slides(self):
        assert self._n(
            '<body><div class="swiper"><div class="swiper-slide">a</div>'
            '<div class="swiper-slide">b</div></div></body>'
        ) == 1

    def test_attribute_marker(self):
        assert self._n('<body><section data-carousel="true">s</section></body>') == 1

    def test_accordion_and_tabs_are_not_sliders(self):
        # slider_count is a strict subset: non-slider components must NOT count here.
        assert self._n('<body><div class="accordion">y</div></body>') == 0
        assert self._n('<body><ul role="tablist"><li>t</li></ul></body>') == 0
        assert self._n('<body><div class="lightbox">z</div></body>') == 0

    def test_slider_is_always_also_a_component(self):
        # Every slider token is in the component set, so a slider counts in both.
        html = '<body><div class="owl-carousel">c</div></body>'
        assert self._n(html) == 1
        assert ce.component_signals(lxml_html.fromstring(html))["component_count"] == 1

    def test_orphan_slide_not_matched(self):
        assert self._n('<body><div class="carousel-item">orphan</div></body>') == 0


class TestStandaloneImageCount:
    """issue #25: standalone_image_count excludes images inside a slider/carousel container,
    so a carousel's images aren't counted as standalone (media-heavy) page images."""

    def _c(self, html):
        return count_structure(
            lxml_html.fromstring(html), "https://example.com/p",
            is_internal=_internal, asset_extensions=ASSET_EXTS,
        )

    def test_carousel_images_excluded_from_standalone(self):
        c = self._c(
            '<body><div class="swiper"><img src="/s1.jpg"><img src="/s2.jpg">'
            '<img src="/s3.jpg"></div><img src="/a.jpg"><img src="/b.jpg"></body>'
        )
        assert c["image_count"] == 5
        assert c["standalone_image_count"] == 2  # the 3 carousel images are excluded

    def test_no_slider_standalone_equals_image_count(self):
        c = self._c('<body><img src="/1.jpg"><img src="/2.jpg"><img src="/3.jpg"></body>')
        assert c["standalone_image_count"] == c["image_count"] == 3

    def test_attribute_marker_slider_excludes_its_images(self):
        # The @data-carousel branch of the slider predicate must also exclude images.
        c = self._c(
            '<body><section data-carousel="true"><img src="/s.jpg"></section>'
            '<img src="/a.jpg"></body>'
        )
        assert c["image_count"] == 2 and c["standalone_image_count"] == 1

    def test_child_part_class_does_not_exclude_its_image(self):
        # A child-part class (`swiper-slide`, not the container token `swiper`) must NOT match,
        # so an image whose only chrome-ish ancestor is a `swiper-slide` counts as standalone.
        c = self._c('<body><div class="swiper-slide"><img src="/x.jpg"></div></body>')
        assert c["standalone_image_count"] == 1

    def test_empty_enrichment_defaults_new_fields_to_zero(self):
        e = ce.empty_enrichment()
        assert e["slider_count"] == 0 and e["standalone_image_count"] == 0


class TestSliderStandaloneScope:
    """issue #25: slider_count is page-wide (body_subtree) while standalone_image_count is
    scoped to the counted content region -- so a slider OUTSIDE the content region is detected
    yet leaves standalone_image_count == image_count (its images aren't in the region at all)."""

    def test_slider_outside_content_region(self):
        # A hero carousel sits in a <header> (site chrome, outside the located <main> region);
        # the content region has only standalone images. slider_count (over body) sees the
        # carousel; standalone_image_count (over the region) equals image_count there.
        html = (
            b"<!DOCTYPE html><html><head><title>T</title></head><body>"
            b"<header><div class='swiper'><img src='/h1.jpg'><img src='/h2.jpg'></div></header>"
            b"<main><h1>Gallery</h1><p>The rocky shelves along the northern coast hold dozens of "
            b"tide pools that fill and drain with each turning of the sea revealing anemones and "
            b"crabs to anyone who arrives before the lowest tide of the day here.</p>"
            b"<img src='/a.jpg'><img src='/b.jpg'></main></body></html>"
        )
        result = extract_content(html)
        counts = count_structure(
            result.subtree, "https://example.com/p", is_internal=_internal, asset_extensions=ASSET_EXTS
        )
        # Content region: 2 standalone images, no in-region slider -> standalone == image_count.
        assert counts["image_count"] == 2 and counts["standalone_image_count"] == 2
        # But the page-wide slider signal still detects the hero carousel.
        assert ce.slider_signals(result.body_subtree)["slider_count"] == 1


class TestScriptSignals:
    """issue #28: third-party integrations via external <script src> hosts. Chat/forms/
    booking/CRM widgets inject via script tags (not iframes) -- the surprise-integration
    blind spot. Routine hosts (analytics/CDN/fonts) and the site's own scripts don't count."""

    from script_allowlist import load_benign_script_hosts as _lbsh
    BENIGN = _lbsh()
    SELF = frozenset({"example.com", "www.example.com"})

    def _s(self, body):
        return ce.script_signals(body, self.BENIGN, self.SELF)

    def test_flags_distinct_nonbenign_integrations(self):
        body = (
            b"<html><head>"
            b"<script src='https://widget.intercom.io/w'></script>"
            b"<script src='https://js.hsforms.net/forms/v2.js'></script>"
            b"<script src='https://js.hsforms.net/forms/embed.js'></script>"  # same host, dedup
            b"</head><body>x</body></html>"
        )
        r = self._s(body)
        assert r["script_embed_count_nonbenign"] == 2  # intercom + hsforms (deduped)
        assert r["script_hosts"] == ["widget.intercom.io", "js.hsforms.net"]

    def test_benign_hosts_excluded(self):
        body = (
            b"<html><head>"
            b"<script src='https://code.jquery.com/jquery.js'></script>"
            b"<script src='https://www.googletagmanager.com/gtag/js'></script>"
            b"<script src='https://drift.com/widget.js'></script>"
            b"</head><body>x</body></html>"
        )
        r = self._s(body)
        assert r["script_hosts"] == ["drift.com"]  # routine jQuery/GTM excluded, only the widget
        assert r["script_embed_count_nonbenign"] == 1

    def test_own_and_relative_scripts_skipped(self):
        body = (
            b"<html><head>"
            b"<script src='/js/app.js'></script>"                       # relative
            b"<script src='https://www.example.com/theme/main.js'></script>"  # own host
            b"<script src='https://example.com/x.js'></script>"         # own apex
            b"</head><body>x</body></html>"
        )
        r = self._s(body)
        assert r["script_hosts"] == [] and r["script_embed_count_nonbenign"] == 0

    def test_recaptcha_loader_not_flagged(self):
        # www.google.com/recaptcha/api.js is on virtually every WP form page -- routine, not
        # an integration surprise. (Regression for the allowlist-intent gap.)
        r = self._s(b"<html><head><script src='https://www.google.com/recaptcha/api.js'></script></head><body>x</body></html>")
        assert r["script_hosts"] == [] and r["script_embed_count_nonbenign"] == 0

    def test_polyfill_io_is_flagged(self):
        # polyfill.io (2024 supply-chain compromise) must be surfaced, not allowlisted.
        r = self._s(b"<html><head><script src='https://cdn.polyfill.io/v3/polyfill.min.js'></script></head><body>x</body></html>")
        assert r["script_hosts"] == ["cdn.polyfill.io"] and r["script_embed_count_nonbenign"] == 1

    def test_protocol_relative_host_flagged(self):
        r = self._s(b"<html><body><script src='//cdn.calendly.com/widget.js'></script></body></html>")
        assert r["script_hosts"] == ["cdn.calendly.com"] and r["script_embed_count_nonbenign"] == 1

    def test_inline_and_srcless_scripts_ignored(self):
        r = self._s(b"<html><body><script>var x=1;</script><script></script></body></html>")
        assert r["script_hosts"] == [] and r["script_embed_count_nonbenign"] == 0

    def test_unparseable_and_empty_bodies(self):
        assert self._s(b"")["script_hosts"] == []
        assert self._s(b"\x00\xff not html")["script_embed_count_nonbenign"] == 0

    def test_empty_enrichment_has_script_fields(self):
        e = ce.empty_enrichment()
        assert e["script_hosts"] == [] and e["script_embed_count_nonbenign"] == 0


class TestStructureHash:
    """issue #36: a content-independent fingerprint of the page's layout, over the full body,
    so pages built from the same template cluster regardless of content or item counts."""

    def _doc(self, inner):
        return (b"<!DOCTYPE html><html><head><title>T</title></head><body>"
                b"<header><nav><a>Home</a><a>About</a></nav></header>"
                + inner + b"<footer><a>Privacy</a></footer></body></html>")

    def _h(self, inner):
        return ce.structure_hash(extract_content(self._doc(inner)).body_subtree)

    def _listing(self, n, words):
        cards = b"".join(b"<div class='card'><h3>Item %d</h3><p>%s</p><img src=/x.jpg></div>" % (i, words)
                         for i in range(n))
        return b"<main><h1>Directory</h1><div class='grid'>" + cards + b"</div></main>"

    def _article(self, body):
        return b"<main><article><h1>Title</h1><p>" + body + b"</p><p>" + body + b"</p></article></main>"

    def test_same_template_different_item_count_clusters(self):
        # A listing with 3 vs 12 cards (different content) -> identical fingerprint.
        assert self._h(self._listing(3, b"alpha")) == self._h(self._listing(12, b"quite different text"))

    def test_content_length_independent(self):
        # Same article template, long vs short body -> identical (not sensitive to word count).
        long_body = b"word " * 60
        assert self._h(self._article(long_body)) == self._h(self._article(b"short"))

    def test_distinct_templates_differ(self):
        listing = self._h(self._listing(4, b"x"))
        article = self._h(self._article(b"y"))
        landing = self._h(b"<main><section><div class='swiper'><div>a</div></div></section>"
                          b"<section><table><tr><td>z</td></tr></table></section></main>")
        assert len({listing, article, landing}) == 3

    def test_wrapper_chains_do_not_false_merge(self):
        # Regression: real themes/builders wrap content in div#page > div.site-inner >
        # div.builder > main. The fingerprint must skip chrome + unwrap to the content root,
        # or every template collapses to one chrome-only hash (a silent false-merge).
        def wrap(main):
            return (b"<!DOCTYPE html><html><head><title>T</title></head><body>"
                    b"<div id='page'><header><nav><a>Home</a></nav></header>"
                    b"<div class='site-inner'><div class='builder'>" + main + b"</div></div>"
                    b"<footer><a>Privacy</a></footer></div></body></html>")
        article = self._raw(wrap(b"<main><article><h1>T</h1><p>x</p><blockquote>q</blockquote></article></main>"))
        landing = self._raw(wrap(b"<main><section><div><h2>a</h2><p>b</p></div></section>"
                                 b"<section><div><table><tr><td>c</td></tr></table></div></section></main>"))
        assert article != landing  # distinct templates survive the wrapper chain
        # And same template through the wrapper, different content length -> still clusters.
        a2 = self._raw(wrap(b"<main><article><h1>Different Title Entirely</h1><p>" + b"word " * 50
                            + b"</p><blockquote>q</blockquote></article></main>"))
        assert article == a2

    def _raw(self, doc):
        return ce.structure_hash(extract_content(doc).body_subtree)

    def test_sidebar_is_a_template_discriminator(self):
        # <aside> is content (TOC / related rail / sidebar), NOT chrome: a template WITH a
        # sidebar must not merge with one WITHOUT, and the tag choice (<aside> vs div.sidebar)
        # must not change clustering.
        no_side = self._doc(b"<main><article><h1>T</h1><p>x</p><p>y</p></article></main>")
        aside = self._doc(b"<main><article><h1>T</h1><p>x</p><p>y</p></article>"
                          b"<aside><div><a>Related</a></div></aside></main>")
        div_side = self._doc(b"<main><article><h1>T</h1><p>x</p><p>y</p></article>"
                             b"<div class='sidebar'><div><a>Related</a></div></div></main>")
        h_no, h_aside, h_div = (ce.structure_hash(extract_content(d).body_subtree)
                                for d in (no_side, aside, div_side))
        assert h_aside != h_no and h_div != h_no

    def test_empty_or_structureless_is_blank(self):
        assert ce.structure_hash(lxml_html.fromstring(b"<body></body>")) == ""
        # A body of only inline/text tags (no block structure) has no fingerprint.
        assert ce.structure_hash(lxml_html.fromstring(b"<body><span>hi</span><a>x</a></body>")) == ""

    def test_empty_enrichment_has_blank_structure_hash(self):
        assert ce.empty_enrichment()["structure_hash"] == ""

    def test_deterministic(self):
        a = self._listing(5, b"one")
        assert self._h(a) == self._h(a)


class TestInternalLinkTargets:
    """issue #45: the content-region internal link edge list -- distinct, fragment-stripped,
    with external / in-page-anchor / self-page links excluded, and capped."""

    def _c(self, html):
        return count_structure(
            lxml_html.fromstring(html), "https://example.com/p",
            is_internal=_internal, asset_extensions=ASSET_EXTS,
        )

    def test_distinct_internal_targets_only(self):
        c = self._c(
            "<body>"
            '<a href="/a">a</a><a href="/a#sec">a again</a>'   # one target, fragment stripped
            '<a href="/b">b</a>'
            '<a href="https://other.com/x">ext</a>'            # external -> excluded
            '<a href="#top">anchor</a>'                        # in-page anchor -> excluded
            '<a href="/p">self</a>'                            # self page -> excluded
            "</body>"
        )
        assert set(c["internal_link_targets"]) == {
            "https://example.com/a",
            "https://example.com/b",
        }

    def test_targets_capped(self):
        links = "".join(f'<a href="/p{i}">l</a>' for i in range(150))
        c = self._c(f"<body>{links}</body>")
        assert len(c["internal_link_targets"]) == 100  # _MAX_INTERNAL_TARGETS


class TestExternalLinkHosts:
    """issue #57: distinct external link hostnames (sibling members./portal. subdomains flag a
    gated portal); internal links excluded, deduped, capped."""

    def _c(self, html):
        return count_structure(lxml_html.fromstring(html), "https://example.com/p",
                               is_internal=_internal, asset_extensions=ASSET_EXTS)

    def test_distinct_external_hosts_internal_excluded(self):
        c = self._c(
            "<body>"
            '<a href="https://members.example.com/login">portal</a>'
            '<a href="https://members.example.com/dues">again</a>'   # same host -> 1
            '<a href="/about">internal</a>'                          # internal -> excluded
            '<a href="https://www.facebook.com/x">fb</a>'
            "</body>"
        )
        assert set(c["external_link_hosts"]) == {"members.example.com", "www.facebook.com"}
