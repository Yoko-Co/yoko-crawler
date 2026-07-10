import pytest
from website_spider import WebsiteSpider


@pytest.fixture
def spider():
    return WebsiteSpider(domain="example.com")


class TestIsLoginUrl:
    """Test login/auth URL detection."""

    def test_wp_login(self, spider):
        assert spider.is_login_url("https://example.com/wp-login.php")

    def test_wp_login_with_redirect(self, spider):
        assert spider.is_login_url(
            "https://example.com/wp-login.php?action=imis_login"
            "&redirect_to=https%3A%2F%2Fexample.com%2Fnews%2F"
        )

    def test_wp_admin(self, spider):
        assert spider.is_login_url("https://example.com/wp-admin/")

    def test_wp_admin_subpath(self, spider):
        assert spider.is_login_url("https://example.com/wp-admin/edit.php")

    def test_login_path(self, spider):
        assert spider.is_login_url("https://example.com/login")

    def test_login_trailing_slash(self, spider):
        assert spider.is_login_url("https://example.com/login/")

    def test_nested_login(self, spider):
        assert spider.is_login_url("https://example.com/account/login")

    def test_signin(self, spider):
        assert spider.is_login_url("https://example.com/signin")

    def test_sign_in_hyphen(self, spider):
        assert spider.is_login_url("https://example.com/sign-in")

    def test_sign_in_underscore(self, spider):
        assert spider.is_login_url("https://example.com/sign_in")

    def test_logout(self, spider):
        assert spider.is_login_url("https://example.com/logout")

    def test_auth_path(self, spider):
        assert spider.is_login_url("https://example.com/auth/callback")

    def test_oauth(self, spider):
        assert spider.is_login_url("https://example.com/oauth/authorize")

    def test_oauth2(self, spider):
        assert spider.is_login_url("https://example.com/oauth2/token")

    def test_sso(self, spider):
        assert spider.is_login_url("https://example.com/sso/redirect")

    def test_cas(self, spider):
        assert spider.is_login_url("https://example.com/cas/login")

    def test_saml(self, spider):
        assert spider.is_login_url("https://example.com/saml/acs")

    def test_adfs(self, spider):
        assert spider.is_login_url("https://example.com/adfs/ls")

    def test_case_insensitive(self, spider):
        assert spider.is_login_url("https://example.com/WP-LOGIN.PHP")
        assert spider.is_login_url("https://example.com/Login/")
        assert spider.is_login_url("https://example.com/AUTH/callback")


class TestIsLoginUrlFalsePositives:
    """Ensure legitimate pages are not incorrectly flagged as login URLs."""

    def test_blog_post_with_login_word(self, spider):
        assert not spider.is_login_url("https://example.com/blog/login-page-design")

    def test_product_with_login_word(self, spider):
        assert not spider.is_login_url("https://example.com/products/login-mat")

    def test_news_article(self, spider):
        assert not spider.is_login_url("https://example.com/news/new-login-feature")

    def test_homepage(self, spider):
        assert not spider.is_login_url("https://example.com/")

    def test_about_page(self, spider):
        assert not spider.is_login_url("https://example.com/about")

    def test_blog_index(self, spider):
        assert not spider.is_login_url("https://example.com/blog/")

    def test_authorization_docs(self, spider):
        assert not spider.is_login_url("https://example.com/docs/authorization-guide")

    def test_authenticate_page(self, spider):
        assert not spider.is_login_url("https://example.com/how-to-authenticate")

    def test_sso_integration_article(self, spider):
        assert not spider.is_login_url("https://example.com/blog/sso-integration-tips")


class TestIsInfraUrl:
    """Test WordPress infrastructure URL detection."""

    def test_wp_json_posts(self, spider):
        assert spider.is_infra_url("https://example.com/wp-json/wp/v2/posts/335")

    def test_wp_json_users(self, spider):
        assert spider.is_infra_url("https://example.com/wp-json/wp/v2/users/32")

    def test_wp_json_oembed(self, spider):
        assert spider.is_infra_url("https://example.com/wp-json/oembed/1.0/embed")

    def test_wp_json_root(self, spider):
        assert spider.is_infra_url("https://example.com/wp-json/")

    def test_xmlrpc(self, spider):
        assert spider.is_infra_url("https://example.com/xmlrpc.php")

    def test_wp_cron(self, spider):
        assert spider.is_infra_url("https://example.com/wp-cron.php")

    def test_trackback(self, spider):
        assert spider.is_infra_url("https://example.com/2024/01/my-post/trackback/")

    def test_trackback_root(self, spider):
        assert spider.is_infra_url("https://example.com/trackback/")

    def test_case_insensitive(self, spider):
        assert spider.is_infra_url("https://example.com/WP-JSON/wp/v2/posts/1")
        assert spider.is_infra_url("https://example.com/XMLRPC.PHP")


class TestIsInfraUrlFalsePositives:
    """Ensure legitimate pages are not incorrectly flagged as infra URLs."""

    def test_wp_json_viewer_page(self, spider):
        assert not spider.is_infra_url("https://example.com/wp-json-viewer")

    def test_xmlrpc_article(self, spider):
        assert not spider.is_infra_url("https://example.com/blog/xmlrpc-explained")

    def test_trackback_policy(self, spider):
        assert not spider.is_infra_url("https://example.com/trackback-policy")

    def test_feed_not_filtered(self, spider):
        """Feed URLs are intentionally NOT infra-filtered (handled by plugin)."""
        assert not spider.is_infra_url("https://example.com/feed/")
        assert not spider.is_infra_url("https://example.com/author/ben/feed/")
        assert not spider.is_infra_url("https://example.com/comments/feed/")

    def test_regular_pages(self, spider):
        assert not spider.is_infra_url("https://example.com/")
        assert not spider.is_infra_url("https://example.com/about")
        assert not spider.is_infra_url("https://example.com/blog/my-post")


# ---------------------------------------------------------------------------
# Emit integration: additive NDJSON schema (U5)
# ---------------------------------------------------------------------------

from scrapy.http import HtmlResponse, Request, Response  # noqa: E402

import content_extractor as ce  # noqa: E402
import run_spider  # noqa: E402
from content_extractor import ENRICHMENT_FIELD_NAMES  # noqa: E402

# The original five fields, which must remain unchanged for backward compat.
ORIGINAL_FIELDS = {"url", "status", "last_modified", "redirected_to", "referrer"}

# Every additive enrichment field present on every row (content_text excluded --
# conditional on --emit-content). Derived from the single source of truth so the
# test can never drift from the production field list.
ENRICHMENT_FIELDS = set(ENRICHMENT_FIELD_NAMES)

ARTICLE_PAGE = b"""
<!DOCTYPE html><html lang="en"><head><title>Pools</title></head><body>
<header><a href="/">home</a></header>
<main><article>
<h1>Survey of the Coastal Pools</h1>
<p>The rocky shelves along the northern coast hold dozens of tide pools that
fill and drain with each turning of the sea, revealing anemones and crabs to
anyone who arrives at the hour before the lowest tide along the shoreline.</p>
<p>Naturalists have catalogued these pools for more than a century, returning
season after season to record which species persist and which fade over the
decades of careful and patient observation by volunteers.</p>
<iframe src="https://public.tableau.com/views/x"></iframe>
</article></main>
<footer><a href="/privacy">privacy</a></footer>
</body></html>
"""


def _html_response(body=ARTICLE_PAGE, url="https://example.com/survey"):
    return HtmlResponse(
        url=url,
        body=body,
        headers={"Content-Type": "text/html; charset=utf-8"},
        request=Request(url),
        status=200,
    )


def _asset_response(url="https://example.com/report.pdf"):
    # A HEAD-style asset row: a non-text Response with no HTML body.
    return Response(
        url=url,
        headers={"Content-Type": "application/pdf"},
        request=Request(url, method="HEAD"),
        status=200,
    )


def _emit_one(spider, response):
    rows = list(spider._emit_row(response))
    assert len(rows) == 1
    return rows[0]


class TestEmitSchema:
    def test_original_fields_unchanged(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())
        assert ORIGINAL_FIELDS.issubset(row)
        assert isinstance(row["url"], str)
        assert isinstance(row["status"], int)
        assert isinstance(row["last_modified"], str)
        assert isinstance(row["redirected_to"], str)
        assert isinstance(row["referrer"], str)

    def test_html_row_carries_all_enrichment_fields(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())
        assert ENRICHMENT_FIELDS.issubset(row)
        # The article has a non-benign Tableau embed and real content.
        assert row["embed_count_nonbenign"] == 1
        assert "public.tableau.com" in row["iframe_hosts"]
        assert row["word_count"] > 0
        assert len(row["content_hash"]) == 64

    def test_asset_row_has_default_enrichment(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _asset_response())
        assert row["content_hash"] == ""
        assert row["main_content_extracted"] is False
        assert row["word_count"] == 0
        assert row["iframe_hosts"] == []
        assert row["embed_count_nonbenign"] == 0
        # Same enrichment keys as an HTML row -> consistent shape.
        assert ENRICHMENT_FIELDS.issubset(row)

    def test_html_and_asset_rows_share_key_set(self):
        spider = WebsiteSpider(domain="example.com")
        html_row = _emit_one(WebsiteSpider(domain="example.com"), _html_response())
        asset_row = _emit_one(spider, _asset_response())
        assert set(html_row) == set(asset_row)


class TestEmitContentFlag:
    def test_content_text_absent_by_default(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())
        assert "content_text" not in row

    def test_content_text_present_when_enabled(self):
        spider = WebsiteSpider(domain="example.com", emit_content=1)
        row = _emit_one(spider, _html_response())
        assert "content_text" in row
        assert "tide pools" in row["content_text"].lower()

    def test_hash_identical_regardless_of_emit_content(self):
        off = _emit_one(WebsiteSpider(domain="example.com"), _html_response())
        on = _emit_one(
            WebsiteSpider(domain="example.com", emit_content=1), _html_response()
        )
        assert off["content_hash"] == on["content_hash"]

    def test_asset_content_text_empty_when_enabled(self):
        spider = WebsiteSpider(domain="example.com", emit_content=1)
        row = _emit_one(spider, _asset_response())
        assert row["content_text"] == ""


class TestIframeHostsEncoding:
    def test_jsonlines_emits_native_list(self):
        spider = WebsiteSpider(domain="example.com", output_format="jsonlines")
        row = _emit_one(spider, _html_response())
        assert isinstance(row["iframe_hosts"], list)

    def test_csv_emits_json_encoded_string(self):
        import json

        spider = WebsiteSpider(domain="example.com", output_format="csv")
        row = _emit_one(spider, _html_response())
        assert isinstance(row["iframe_hosts"], str)
        # Round-trips back to the host list.
        assert json.loads(row["iframe_hosts"]) == ["public.tableau.com"]

    def test_csv_with_emit_content_has_both_fields(self):
        spider = WebsiteSpider(
            domain="example.com", output_format="csv", emit_content=1
        )
        row = _emit_one(spider, _html_response())
        assert isinstance(row["iframe_hosts"], str)  # JSON-encoded for CSV
        assert isinstance(row["content_text"], str)
        assert "tide pools" in row["content_text"].lower()


class TestSchemaSync:
    """Guards against drift between the field-list sources of truth."""

    def test_base_feed_fields_match_source(self):
        assert run_spider.BASE_FEED_FIELDS == (
            run_spider.ORIGINAL_FEED_FIELDS + list(ENRICHMENT_FIELD_NAMES)
        )

    def test_empty_enrichment_keys_match_source(self):
        assert set(ce.empty_enrichment()) == set(ENRICHMENT_FIELD_NAMES)

    def test_emitted_html_row_keys_match_feed_fields(self):
        # An emitted HTML row (no --emit-content) must carry exactly the feed
        # columns -- the actual output contract, end to end.
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())
        assert set(row) == set(run_spider.BASE_FEED_FIELDS)

    def test_asset_and_html_rows_share_keys(self):
        html_row = _emit_one(WebsiteSpider(domain="example.com"), _html_response())
        asset_row = _emit_one(WebsiteSpider(domain="example.com"), _asset_response())
        assert set(html_row) == set(asset_row) == set(run_spider.BASE_FEED_FIELDS)


class TestEnrichmentResilience:
    def test_enrichment_failure_still_emits_row_with_original_fields(self, monkeypatch):
        # If extraction raises, the row must still emit with the original five
        # fields intact and empty enrichment defaults (backward-compat guarantee).
        def _boom(*a, **k):
            raise RuntimeError("extraction exploded")

        monkeypatch.setattr("website_spider.extract_content", _boom)
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())
        assert ORIGINAL_FIELDS.issubset(row)
        assert row["url"]
        assert row["status"] == 200
        # Enrichment degraded to defaults rather than dropping the row.
        assert row["content_hash"] == ""
        assert row["word_count"] == 0
        assert row["iframe_hosts"] == []
        assert ENRICHMENT_FIELDS.issubset(row)

    def test_footer_embed_counted_page_wide(self):
        # A surprise embed in the footer (outside the main region) is still
        # flagged -- the signal is page-wide.
        html = (
            b"<html><body>"
            b"<main><article><h1>Survey of the Coastal Pools</h1>"
            b"<p>The rocky shelves along the northern coast hold dozens of tide "
            b"pools that fill and drain with each turning of the sea, revealing "
            b"anemones and crabs to anyone arriving before the lowest tide.</p>"
            b"<p>Naturalists have catalogued these pools for over a century, "
            b"season after season, recording which species persist and fade.</p>"
            b"</article></main>"
            b"<footer><iframe src='https://public.tableau.com/v/x'></iframe></footer>"
            b"</body></html>"
        )
        resp = HtmlResponse(
            url="https://example.com/survey",
            body=html,
            headers={"Content-Type": "text/html"},
            request=Request("https://example.com/survey"),
            status=200,
        )
        row = _emit_one(WebsiteSpider(domain="example.com"), resp)
        assert row["main_content_extracted"] is True
        assert row["embed_count_nonbenign"] == 1
        assert "public.tableau.com" in row["iframe_hosts"]


class TestEnrichmentGating:
    def test_redirect_html_gets_default_enrichment(self):
        # A 301 with an HTML body is not a content page -> empty enrichment.
        resp = HtmlResponse(
            url="https://example.com/old",
            body=b"<html><body><p>Moved</p></body></html>",
            headers={"Content-Type": "text/html", "Location": "/new"},
            request=Request("https://example.com/old"),
            status=301,
        )
        row = _emit_one(WebsiteSpider(domain="example.com"), resp)
        assert row["main_content_extracted"] is False
        assert row["content_hash"] == ""
        assert row["word_count"] == 0

    def test_non_html_textresponse_gets_default_enrichment(self):
        # text/xml (e.g. a sitemap) is not enriched.
        from scrapy.http import TextResponse

        resp = TextResponse(
            url="https://example.com/sitemap.xml",
            body=b"<urlset><url><loc>https://example.com/</loc></url></urlset>",
            headers={"Content-Type": "text/xml"},
            request=Request("https://example.com/sitemap.xml"),
            status=200,
        )
        row = _emit_one(WebsiteSpider(domain="example.com"), resp)
        assert row["main_content_extracted"] is False
        assert row["iframe_hosts"] == []
        assert ENRICHMENT_FIELDS.issubset(row)


class TestUnwantedParamStripping:
    """Query-param dedup (issue #8): non-content params collapse to the base URL, both
    when emitting (so /x/ and /x/?s= aren't two pages) and when scheduling (so the ?s=
    variant isn't even crawled). Meaningful params survive."""

    def _emit(self, spider, url):
        return spider.normalize_url(url, exclude_params=spider.exclude_params_emit)

    def _schedule(self, spider, url):
        return spider.normalize_url(url, exclude_params=spider.exclude_params_schedule)

    def test_empty_wp_search_collapses_on_emit(self, spider):
        # The GVF doubling: every page appeared as /x/ AND /x/?s=.
        assert self._emit(spider, "https://example.com/x/?s=") == self._emit(spider, "https://example.com/x/")

    def test_wp_search_with_value_collapses(self, spider):
        # Search-results variants are not content pages -> collapse any ?s= value.
        assert self._emit(spider, "https://example.com/x/?s=vitiligo") == self._emit(spider, "https://example.com/x/")

    def test_s_is_stripped_from_scheduling_too(self, spider):
        # Not just deduped on emit -- the ?s= variant normalizes to the base for scheduling,
        # so the dupefilter treats it as already-seen and it is never crawled.
        assert "s" in spider.exclude_params_schedule
        assert self._schedule(spider, "https://example.com/x/?s=") == self._schedule(spider, "https://example.com/x/")

    def test_wp_comment_params_collapse(self, spider):
        assert self._emit(spider, "https://example.com/post/?replytocom=42") == self._emit(spider, "https://example.com/post/")

    def test_meaningful_param_is_preserved(self, spider):
        # A curated denylist only -- an unknown param (e.g. a store item id) must survive.
        out = self._emit(spider, "https://example.com/product/?product_id=5")
        assert "product_id=5" in out

    def test_search_param_appended_to_pdf_collapses(self, spider):
        # GVF even appended ?s= to a PDF URL; the variant must collapse onto the asset.
        assert self._emit(spider, "https://example.com/file.pdf?s=") == self._emit(spider, "https://example.com/file.pdf")

    def test_s_is_case_insensitive(self, spider):
        # key.lower() -> ?S= collapses too.
        assert self._emit(spider, "https://example.com/x/?S=") == self._emit(spider, "https://example.com/x/")

    def test_search_key_collapses(self, spider):
        assert self._emit(spider, "https://example.com/x/?search=vitiligo") == self._emit(spider, "https://example.com/x/")

    def test_denylisted_dropped_while_sibling_kept(self, spider):
        # The drop-one-keep-the-other path: ?s= dropped, a meaningful param survives.
        out = self._emit(spider, "https://example.com/x/?s=q&id=5")
        assert "id=5" in out and "s=q" not in out


class TestNavigationalHref:
    """issue #11: only http(s)-navigational hrefs become crawl targets. Non-navigational
    schemes -- including MALFORMED ones a space/%20 would smuggle past urljoin as a path --
    are skipped."""

    def test_well_formed_mailto_rejected(self, spider):
        assert spider.is_navigational_href("mailto:info@example.com") is False

    def test_space_mangled_mailto_rejected(self, spider):
        # The GVF case: <a href="mail to:info@x"> would urljoin to '.../mail%20to:info@x'.
        assert spider.is_navigational_href("mail to:info@example.com") is False

    def test_percent20_mangled_mailto_rejected(self, spider):
        assert spider.is_navigational_href("mail%20to:info@example.com") is False

    def test_encoded_whitespace_and_bom_mangled_mailto_rejected(self, spider):
        # Review hardening: %09/%0a encoded whitespace and a leading BOM also collapse.
        for h in ("mail%09to:info@x", "mail%0Ato:info@x", "\ufeffmailto:info@x"):
            assert spider.is_navigational_href(h) is False, h

    def test_mailto_case_insensitive(self, spider):
        assert spider.is_navigational_href("MAILTO:X@Y.COM") is False

    def test_other_nonnav_schemes_rejected(self, spider):
        for h in ("tel:+15551234", "sms:15551234", "javascript:void(0)", "data:text/html,x", "callto:x", "file:///etc"):
            assert spider.is_navigational_href(h) is False, h

    def test_empty_and_fragment_rejected(self, spider):
        assert spider.is_navigational_href("") is False
        assert spider.is_navigational_href(None) is False
        assert spider.is_navigational_href("#section") is False

    def test_normal_urls_are_navigational(self, spider):
        for h in ("/about", "https://example.com/x", "http://example.com/y", "articles/1", "../up"):
            assert spider.is_navigational_href(h) is True, h

    def test_space_in_path_is_navigational(self, spider):
        # A space in the PATH (not a mangled scheme) is a real URL -- collapse is only for
        # scheme detection; the real urljoin handles the space.
        assert spider.is_navigational_href("/files/my report.pdf") is True

    def test_tel_as_path_segment_is_navigational(self, spider):
        # '/tel/...' is a path, not the tel: scheme.
        assert spider.is_navigational_href("/tel/directory") is True


def _html_with_canonical(canon_href, url="https://example.com/page"):
    body = (
        '<html><head><link rel="canonical" href="' + canon_href + '">'
        '<title>t</title></head><body><main><article><p>'
        + "word " * 60 +
        '</p></article></main></body></html>'
    ).encode("utf-8")
    return _html_response(body=body, url=url)


class TestCanonical:
    """issue #10: the page's <link rel=canonical> is emitted, normalized like any URL, so
    yoko-corpus can collapse query-string/variant URLs onto their canonical page."""

    def test_absolute_canonical_normalized(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_with_canonical("https://example.com/canonical-page"))
        assert row["canonical"] == "https://example.com/canonical-page"

    def test_relative_canonical_resolved(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_with_canonical("/canon", url="https://example.com/some/path"))
        assert row["canonical"] == "https://example.com/canon"

    def test_canonical_junk_params_stripped(self):
        # Normalized with the same emit rules -> the ?s= that #8 strips is stripped here too,
        # so a canonical pointing at a clean page compares equal to that page's emit URL.
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_with_canonical("https://example.com/p?utm_source=x&s="))
        assert row["canonical"] == "https://example.com/p"

    def test_absent_canonical_is_empty(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())  # ARTICLE_PAGE has no canonical
        assert row["canonical"] == ""

    def test_asset_row_canonical_empty(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _asset_response())
        assert row["canonical"] == ""

    def test_self_referential_canonical_equals_emit_url(self):
        # Load-bearing for corpus #26: a page canonical'd to itself must yield
        # canonical == the emitted url field (same normalization on both sides).
        spider = WebsiteSpider(domain="example.com")
        url = "https://example.com/page/?utm_source=x"
        row = _emit_one(spider, _html_with_canonical("https://example.com/page/", url=url))
        assert row["canonical"] == row["url"]

    def test_multitoken_and_uppercase_rel_still_match(self):
        # Fresh spider per case (same URL would be deduped by _emit_row's `emitted` set).
        for rel in ["canonical alternate", "CANONICAL"]:
            spider = WebsiteSpider(domain="example.com")
            body = (
                '<html><head><link rel="' + rel + '" href="https://example.com/a">'
                '</head><body><main><article><p>' + "word " * 60 +
                '</p></article></main></body></html>'
            ).encode("utf-8")
            row = _emit_one(spider, _html_response(body=body, url="https://example.com/p"))
            assert row["canonical"] == "https://example.com/a", rel

    def test_canonical_emitted_even_when_extraction_fails(self, monkeypatch):
        # Independence: a canonical is still emitted alongside empty/zero counts when the
        # body extraction raises.
        import content_extractor as ce
        monkeypatch.setattr(ce, "extract_content", lambda body: (_ for _ in ()).throw(RuntimeError("boom")))
        import website_spider as ws
        monkeypatch.setattr(ws, "extract_content", ce.extract_content)
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_with_canonical("https://example.com/canon"))
        assert row["canonical"] == "https://example.com/canon"
        assert row["content_hash"] == "" and row["word_count"] == 0  # counts defaulted
