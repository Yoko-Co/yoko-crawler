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

    def test_cdn_cgi_content(self, spider):
        # Cloudflare AI Labyrinth crawler-trap pages live under /cdn-cgi/content.
        assert spider.is_infra_url(
            "https://example.com/cdn-cgi/content?id=IBljj_TV_pz3NpmQ5siu07t7"
        )

    def test_cdn_cgi_email_protection(self, spider):
        assert spider.is_infra_url("https://example.com/cdn-cgi/l/email-protection")

    def test_cdn_cgi_case_insensitive(self, spider):
        assert spider.is_infra_url("https://example.com/CDN-CGI/content?id=x")


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

    def test_cdn_prefixed_path_not_filtered(self, spider):
        # A real path that merely starts with "cdn" is not the reserved cdn-cgi segment.
        assert not spider.is_infra_url("https://example.com/cdn-guide")
        assert not spider.is_infra_url("https://example.com/blog/cdn-cgi-explained")


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

    def test_html_row_carries_component_count(self):
        # issue #12: a page with a slider emits component_count on the row.
        body = (
            '<html><head><title>t</title></head><body><main><article><p>'
            + "word " * 60 +
            '</p></article></main><div class="swiper"><div class="swiper-slide">a</div></div>'
            '</body></html>'
        ).encode("utf-8")
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response(body=body, url="https://example.com/slides"))
        assert row["component_count"] == 1

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

    def test_csv_json_encodes_internal_link_targets(self):
        # issue #45: the edge list is a peer of iframe_hosts/script_hosts -- it must also survive
        # CSV export as a JSON string, not a comma-mangled raw list.
        import json

        spider = WebsiteSpider(domain="example.com", output_format="csv")
        row = _emit_one(spider, _html_response())
        assert isinstance(row["internal_link_targets"], str)
        assert isinstance(json.loads(row["internal_link_targets"]), list)

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
        # skip_reason (issue #43) sits between the originals and the enrichment columns.
        assert run_spider.BASE_FEED_FIELDS == (
            run_spider.ORIGINAL_FEED_FIELDS + ["skip_reason"] + list(ENRICHMENT_FIELD_NAMES)
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


# ---------- WAF / bot-wall challenge handling ----------

import types  # noqa: E402


class _FakeStats:
    def __init__(self):
        self.values = {}

    def inc_value(self, key, count=1, start=0):
        self.values[key] = self.values.get(key, start) + count

    # Scrapy's real StatsCollector has this; the fake needs it too or a set_value the code
    # under test makes is swallowed by a defensive except and the test passes on a no-op.
    def set_value(self, key, value):
        self.values[key] = value

    def get_value(self, key, default=None):
        return self.values.get(key, default)


def _challenge_response(status=403, url="https://example.com/",
                        body=ARTICLE_PAGE, cloudflare=True, cf_mitigated=False):
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if cloudflare:
        headers["cf-ray"] = "8ab1122334455-EWR"
        headers["Server"] = "cloudflare"
    if cf_mitigated:
        headers["cf-mitigated"] = "challenge"
    return HtmlResponse(url=url, body=body, headers=headers,
                        request=Request(url), status=status)


class TestWafChallenge:
    def test_cloudflare_403_is_a_challenge(self):
        spider = WebsiteSpider(domain="example.com")
        assert spider._is_waf_challenge(_challenge_response(status=403)) is True

    def test_cf_mitigated_header_is_a_challenge(self):
        spider = WebsiteSpider(domain="example.com")
        # 403 + cf-mitigated, even without server:cloudflare, is a challenge.
        resp = _challenge_response(status=403, cloudflare=False, cf_mitigated=True)
        assert spider._is_waf_challenge(resp) is True

    def test_ordinary_403_without_cloudflare_is_not_a_challenge(self):
        spider = WebsiteSpider(domain="example.com")
        resp = HtmlResponse(url="https://example.com/secret", body=b"<html>no</html>",
                            headers={"Content-Type": "text/html"},
                            request=Request("https://example.com/secret"), status=403)
        assert spider._is_waf_challenge(resp) is False

    def test_200_is_never_a_challenge_even_behind_cloudflare(self):
        spider = WebsiteSpider(domain="example.com")
        assert spider._is_waf_challenge(_challenge_response(status=200)) is False

    def test_challenge_page_gets_empty_enrichment(self):
        # The challenge markup must NOT be mined as content -- a real word_count /
        # content_hash would make a bot-blocked page look like a real "simple" page.
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _challenge_response(status=403))
        assert row["status"] == 403
        assert row["content_hash"] == "" and row["word_count"] == 0
        # A normal 200 with the SAME body DOES get real content (contrast).
        normal = _emit_one(WebsiteSpider(domain="example.com"),
                           _html_response(body=ARTICLE_PAGE))
        assert normal["word_count"] > 0

    def test_challenge_page_links_are_not_followed(self):
        spider = WebsiteSpider(domain="example.com")
        spider.crawler = types.SimpleNamespace(stats=_FakeStats())
        body = (b"<html><body><a href='/?ki-cf-botcl=1'>verify</a>"
                b"<a href='/real-page'>real</a></body></html>")
        resp = _challenge_response(status=403, body=body)
        out = list(spider.parse(resp))
        # The row is still emitted, but no link Requests are scheduled off the wall page.
        assert any(isinstance(o, dict) for o in out)
        assert not any(isinstance(o, Request) for o in out)
        assert spider.crawler.stats.values.get("waf_challenge_count") == 1

    def test_normal_page_links_are_followed(self):
        # Control: an ordinary page yields link Requests (challenge guard doesn't fire).
        spider = WebsiteSpider(domain="example.com")
        spider.crawler = types.SimpleNamespace(stats=_FakeStats())
        body = b"<html><body><a href='/real-page'>real</a></body></html>"
        resp = _html_response(body=body, url="https://example.com/")
        out = list(spider.parse(resp))
        assert any(isinstance(o, Request) for o in out)

    def test_challenge_query_params_are_stripped(self):
        # ki-cf-botcl (and Cloudflare challenge tokens) are junk params -> normalized away.
        spider = WebsiteSpider(domain="example.com")
        assert "ki-cf-botcl" in spider.UNWANTED_PARAMS
        normalized = spider.normalize_url(
            "https://example.com/x?ki-cf-botcl=1", exclude_params=spider.exclude_params_emit
        )
        assert "ki-cf-botcl" not in normalized


class TestOriginVsChallenge403:
    """A 403 the ORIGIN generated (member-restricted content) must stay OUT of the bot-wall
    bucket on Cloudflare-fronted sites, where every response also carries cf-ray/server.
    cf-mitigated is the reliable challenge signal; an origin fingerprint marks restricted
    content. (INCOSE's /setdb-login/ returns `403 cf-mitigated: challenge` to a bot.)"""

    def _resp(self, headers, status=403, url="https://example.com/members/"):
        h = {"Content-Type": "text/html; charset=utf-8"}
        h.update(headers)
        return HtmlResponse(url=url, body=b"<html><body>nope</body></html>",
                            headers=h, request=Request(url), status=status)

    def test_cf_mitigated_is_always_a_challenge(self):
        spider = WebsiteSpider(domain="example.com")
        r = self._resp({"cf-ray": "x-EWR", "Server": "cloudflare",
                        "cf-mitigated": "challenge", "x-powered-by": "ASP.NET"})
        # Even WITH an origin header, cf-mitigated wins -- CF explicitly walled it.
        assert spider._is_waf_challenge(r) is True

    def test_cf_fronted_origin_403_with_powered_by_is_not_a_challenge(self):
        spider = WebsiteSpider(domain="example.com")
        r = self._resp({"cf-ray": "x-EWR", "Server": "cloudflare", "x-powered-by": "ASP.NET"})
        assert spider._is_waf_challenge(r) is False

    def test_cf_fronted_origin_403_with_session_cookie_is_not_a_challenge(self):
        spider = WebsiteSpider(domain="example.com")
        r = self._resp({"cf-ray": "x-EWR", "Server": "cloudflare",
                        "Set-Cookie": "ARRAffinity=abc123; Path=/; HttpOnly"})
        assert spider._is_waf_challenge(r) is False

    def test_cf_wall_403_without_origin_headers_is_a_challenge(self):
        # A real CF wall carries no origin fingerprint -> still caught by the fallback.
        spider = WebsiteSpider(domain="example.com")
        r = self._resp({"cf-ray": "x-EWR", "Server": "cloudflare"})
        assert spider._is_waf_challenge(r) is True

    def test_cf_own_cookie_is_not_an_origin_fingerprint(self):
        # __cf_bm is Cloudflare's own cookie -> not an origin signal, so this stays a wall.
        spider = WebsiteSpider(domain="example.com")
        r = self._resp({"cf-ray": "x-EWR", "Server": "cloudflare",
                        "Set-Cookie": "__cf_bm=xyz; Path=/"})
        assert spider._is_waf_challenge(r) is True

    def test_has_origin_fingerprint_direct(self):
        spider = WebsiteSpider(domain="example.com")
        assert spider._has_origin_fingerprint(self._resp({"x-generator": "Drupal 10"}))
        assert not spider._has_origin_fingerprint(
            self._resp({"cf-ray": "x-EWR", "Server": "cloudflare"}))


class TestForbiddenCounts:
    """parse() records the challenge-vs-origin split as observable stats."""

    def _counts(self, headers):
        spider = WebsiteSpider(domain="example.com")
        spider.crawler = types.SimpleNamespace(stats=_FakeStats())
        h = {"Content-Type": "text/html"}
        h.update(headers)
        resp = HtmlResponse(url="https://example.com/x", body=b"<html></html>",
                            headers=h, request=Request("https://example.com/x"), status=403)
        list(spider.parse(resp))
        return spider.crawler.stats.values

    def test_challenge_403_counts_as_waf_not_origin(self):
        vals = self._counts({"cf-ray": "x", "Server": "cloudflare", "cf-mitigated": "challenge"})
        assert vals.get("waf_challenge_count") == 1
        assert vals.get("origin_forbidden_count") is None

    def test_origin_403_counts_as_forbidden_not_waf(self):
        vals = self._counts({"cf-ray": "x", "Server": "cloudflare", "x-powered-by": "ASP.NET"})
        assert vals.get("origin_forbidden_count") == 1
        assert vals.get("waf_challenge_count") is None


class TestObeyLinkDirectives:
    """The crawler follows only links the site permits: rel='nofollow'/'ugc'/'sponsored'
    anchors, pages marked <meta name='robots' content='nofollow'>, and Cloudflare's
    reserved /cdn-cgi/ path are all left alone. This is what keeps us out of the AI
    Labyrinth trap and, more broadly, off links the site asked bots not to follow."""

    def _parse(self, body, url="https://example.com/"):
        spider = WebsiteSpider(domain="example.com")
        spider.crawler = types.SimpleNamespace(stats=_FakeStats())
        out = list(spider.parse(_html_response(body=body, url=url)))
        followed = [o.url for o in out if isinstance(o, Request)]
        return spider, followed

    def test_rel_nofollow_anchor_skipped(self):
        body = (b"<html><body>"
                b"<a href='/keep'>keep</a>"
                b"<a href='/trap' rel='nofollow'>trap</a>"
                b"</body></html>")
        spider, followed = self._parse(body)
        assert any(u.endswith("/keep") for u in followed)
        assert not any(u.endswith("/trap") for u in followed)
        assert spider.crawler.stats.values.get("nofollow_links_skipped") == 1

    def test_nofollow_among_multiple_rel_tokens(self):
        body = b"<html><body><a href='/x' rel='noopener nofollow'>x</a></body></html>"
        _, followed = self._parse(body)
        assert not any(u.endswith("/x") for u in followed)

    def test_ugc_and_sponsored_also_skipped(self):
        body = (b"<html><body>"
                b"<a href='/u' rel='ugc'>u</a>"
                b"<a href='/s' rel='sponsored'>s</a></body></html>")
        _, followed = self._parse(body)
        assert followed == []

    def test_meta_robots_nofollow_skips_all_links(self):
        body = (b"<html><head><meta name='robots' content='noindex, nofollow'></head>"
                b"<body><a href='/a'>a</a><a href='/b'>b</a></body></html>")
        spider, followed = self._parse(body)
        assert followed == []
        assert spider.crawler.stats.values.get("meta_nofollow_pages") == 1

    def test_meta_robots_none_shorthand_skips_all_links(self):
        body = (b"<html><head><meta name='ROBOTS' content='none'></head>"
                b"<body><a href='/a'>a</a></body></html>")
        _, followed = self._parse(body)
        assert followed == []

    def test_meta_robots_index_follow_permits_links(self):
        body = (b"<html><head><meta name='robots' content='index, follow'></head>"
                b"<body><a href='/a'>a</a></body></html>")
        _, followed = self._parse(body)
        assert any(u.endswith("/a") for u in followed)

    def test_cdn_cgi_link_not_scheduled_end_to_end(self):
        # A plain, followable link into /cdn-cgi/ is still dropped by _schedule's infra gate.
        body = (b"<html><body>"
                b"<a href='/cdn-cgi/content?id=abc'>trap</a>"
                b"<a href='/real'>real</a></body></html>")
        spider, followed = self._parse(body)
        assert any(u.endswith("/real") for u in followed)
        assert not any("/cdn-cgi/" in u for u in followed)
        assert spider.crawler.stats.values.get("infra_urls_skipped") == 1


from scrapy.http import TextResponse  # noqa: E402


def _redirect_response(url, location, status=301, cb_response=Response):
    return cb_response(
        url=url, status=status,
        headers={"Location": location},
        request=Request(url),
    )


def _requests(rows):
    return [r for r in rows if isinstance(r, Request)]


class TestInfraRedirectsStayOnDomain:
    """Issue corpus#71: robots/sitemap must not follow a redirect or a listed sitemap URL
    off-domain -- that fetches another site's infra (the cross-domain contamination class)."""

    def test_robots_redirect_off_domain_not_followed(self):
        spider = WebsiteSpider(domain="example.com")
        resp = _redirect_response("https://example.com/robots.txt",
                                  "https://evil.cdn.net/robots.txt")
        reqs = _requests(spider.parse_robots(resp))
        # The off-domain robots.txt is not fetched. The start URL IS emitted here (issue
        # #76): this redirect ends the robots chain, and seeding now hangs off parse_robots,
        # so returning nothing would leave the crawl with no pages at all. Sitemap probes
        # also fire, because a robots.txt we couldn't read named no sitemap (#77).
        assert "https://example.com/" in [r.url for r in reqs]
        assert not any("evil.cdn.net" in r.url for r in reqs)

    def test_robots_redirect_on_domain_followed(self):
        spider = WebsiteSpider(domain="example.com")
        resp = _redirect_response("http://example.com/robots.txt",
                                  "https://www.example.com/robots.txt")
        reqs = _requests(spider.parse_robots(resp))
        assert [r.url for r in reqs] == ["https://www.example.com/robots.txt"]

    def test_robots_lists_off_domain_sitemap_not_scheduled(self):
        spider = WebsiteSpider(domain="example.com")
        body = (b"User-agent: *\n"
                b"Sitemap: https://cdn.thirdparty.com/sitemap.xml\n"
                b"Sitemap: https://example.com/sitemap.xml\n")
        resp = TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"), status=200)
        urls = [r.url for r in _requests(spider.parse_robots(resp))]
        # On-domain sitemap only; the third-party one is never scheduled. The trailing entry
        # is the start URL, now emitted from parse_robots (issue #76).
        assert urls == ["https://example.com/sitemap.xml", "https://example.com/"]
        assert not any("thirdparty" in u for u in urls)

    def test_sitemap_redirect_off_domain_not_followed(self):
        spider = WebsiteSpider(domain="example.com")
        resp = _redirect_response("https://example.com/sitemap.xml",
                                  "https://other.org/sitemap.xml")
        reqs = _requests(spider.parse_sitemap(resp))
        assert reqs == []

    def test_sitemap_redirect_on_domain_followed(self):
        spider = WebsiteSpider(domain="example.com")
        resp = _redirect_response("https://example.com/sitemap.xml",
                                  "https://example.com/sitemap-1.xml")
        reqs = _requests(spider.parse_sitemap(resp))
        assert [r.url for r in reqs] == ["https://example.com/sitemap-1.xml"]

    def test_redirect_relative_location_resolves_and_is_followed(self):
        # A relative Location (the common same-site form) resolves via urljoin against the response
        # URL and stays internal -- must still be followed.
        spider = WebsiteSpider(domain="example.com")
        resp = _redirect_response("https://example.com/sitemap.xml", "/sitemap-1.xml")
        reqs = _requests(spider.parse_sitemap(resp))
        assert [r.url for r in reqs] == ["https://example.com/sitemap-1.xml"]

    def test_subdomain_redirect_respects_include_subdomains(self):
        # A redirect to a sibling subdomain: dropped by default, followed when subdomains are in scope
        # (guards the is_internal subdomain branch these guards depend on).
        target = "https://blog.example.com/sitemap.xml"
        default = WebsiteSpider(domain="example.com")
        assert _requests(default.parse_sitemap(
            _redirect_response("https://example.com/sitemap.xml", target))) == []
        subs = WebsiteSpider(domain="example.com", include_subdomains="1")
        followed = _requests(subs.parse_sitemap(
            _redirect_response("https://example.com/sitemap.xml", target)))
        assert [r.url for r in followed] == [target]


class TestFacetFamily:
    """Facet-param shape detection (issue #49) -- the gate that decides which URLs the
    depth cap and order-insensitive dedup are allowed to touch."""

    @pytest.mark.parametrize("key,expected", [
        ("f[0]", "f[]"),
        ("f[12]", "f[]"),
        ("tid[2]", "tid[]"),
        ("field_topics[1]", "field_topics[]"),
        ("fq", "fq"),
        ("FACET", "facet"),
        ("filters", "filters"),
    ])
    def test_facet_shapes_recognized(self, spider, key, expected):
        assert spider.facet_family(key) == expected

    @pytest.mark.parametrize("key", [
        "id", "product", "sku", "page_id", "q", "color", "lang",
        "f",            # bare `f` is not a known facet name -- too generic to assume
        "f[]",          # no index -> no slot to reorder
        "f[a]",         # non-numeric index is a keyed map, not a slot
        "prefix[0]suffix",  # bracket not at the end
    ])
    def test_identity_params_are_not_facets(self, spider, key):
        """An identity param must never be treated as a facet, or two genuinely
        different product pages could collapse onto one key."""
        assert spider.facet_family(key) is None


class TestFacetDepth:
    def test_no_query_is_zero(self, spider):
        assert spider.facet_depth("https://example.com/search") == 0

    def test_identity_params_count_zero(self, spider):
        """?id=5&color=red is a product page, not a filter stack -- never capped."""
        assert spider.facet_depth("https://example.com/p?id=5&color=red") == 0

    def test_counts_only_facet_params(self, spider):
        url = "https://example.com/s?q=hats&f[0]=a&f[1]=b"
        assert spider.facet_depth(url) == 2

    def test_real_naeyc_url(self, spider):
        url = ("https://www.naeyc.org/search/equity%20and%20diversity"
               "?f%5B0%5D=field_topics%3A187&f%5B1%5D=field_topics%3A185"
               "&f%5B2%5D=field_topics%3A79")
        assert spider.facet_depth(url) == 3


class TestFacetDedupKey:
    """The order-insensitive scheduling identity. `canonicalize_url` sorts by param NAME,
    so f[0]/f[1] permutations slip past it -- this is what collapses them."""

    def test_permutations_collapse(self, spider):
        a = spider.facet_dedup_key("https://example.com/s?f[0]=a&f[1]=b&f[2]=c")
        b = spider.facet_dedup_key("https://example.com/s?f[0]=c&f[1]=a&f[2]=b")
        c = spider.facet_dedup_key("https://example.com/s?f[0]=b&f[1]=c&f[2]=a")
        assert a == b == c

    def test_different_selections_stay_distinct(self, spider):
        """Reordering collapses; changing WHICH filters are on must not."""
        a = spider.facet_dedup_key("https://example.com/s?f[0]=a&f[1]=b")
        b = spider.facet_dedup_key("https://example.com/s?f[0]=a&f[1]=z")
        assert a != b

    def test_subset_is_distinct_from_superset(self, spider):
        a = spider.facet_dedup_key("https://example.com/s?f[0]=a")
        b = spider.facet_dedup_key("https://example.com/s?f[0]=a&f[1]=b")
        assert a != b

    def test_separate_families_do_not_merge(self, spider):
        """f[0]=a&g[0]=b must not read the same as f[0]=b&g[0]=a."""
        a = spider.facet_dedup_key("https://example.com/s?f[0]=a&g[0]=b")
        b = spider.facet_dedup_key("https://example.com/s?f[0]=b&g[0]=a")
        assert a != b

    def test_non_facet_url_returned_unchanged(self, spider):
        """No facet params -> byte-identical passthrough, so ordinary pages keep their
        exact identity and nothing downstream shifts."""
        url = "https://example.com/p?id=5&color=red"
        assert spider.facet_dedup_key(url) == url

    def test_identity_params_never_collapse(self, spider):
        a = spider.facet_dedup_key("https://example.com/p?id=5")
        b = spider.facet_dedup_key("https://example.com/p?id=6")
        assert a != b

    def test_path_is_preserved(self, spider):
        """Two different search terms are two different pages."""
        a = spider.facet_dedup_key("https://example.com/search/hats?f[0]=a")
        b = spider.facet_dedup_key("https://example.com/search/caps?f[0]=a")
        assert a != b

    def test_non_facet_params_survive_alongside_facets(self, spider):
        """The search TERM (?q=) must survive the rewrite -- dropping it would merge
        every search on the site into one page."""
        key = spider.facet_dedup_key("https://example.com/s?q=hats&f[0]=a")
        assert "q=hats" in key


class TestFacetScheduling:
    """_schedule's two new gates, end to end."""

    def _spider(self, **kw):
        import types
        s = WebsiteSpider(domain="example.com", **kw)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_deep_facet_url_is_skipped(self):
        s = self._spider()
        reqs = list(s._schedule("https://example.com/s?f[0]=a&f[1]=b&f[2]=c"))
        assert reqs == []
        assert s.crawler.stats.values.get("facet_urls_skipped") == 1

    def test_shallow_facet_url_is_scheduled(self):
        s = self._spider()
        reqs = list(s._schedule("https://example.com/s?f[0]=a&f[1]=b"))
        assert len(reqs) == 1

    def test_capped_url_is_not_marked_seen(self):
        """A URL dropped by the cap must not poison `seen` -- raising the cap on a later
        run (or a shallower sibling) must still be crawlable."""
        s = self._spider()
        list(s._schedule("https://example.com/s?f[0]=a&f[1]=b&f[2]=c"))
        assert s.seen == set()

    def test_permutation_is_not_refetched(self):
        s = self._spider()
        first = list(s._schedule("https://example.com/s?f[0]=a&f[1]=b"))
        second = list(s._schedule("https://example.com/s?f[0]=b&f[1]=a"))
        assert len(first) == 1
        assert second == [], "a reordering of the same selection must not be refetched"

    def test_scheduled_url_is_real_not_the_dedup_key(self):
        """We must request the URL as the site emitted it. The dedup key reorders facet
        slots into a canonical form the site may not serve."""
        s = self._spider()
        req = list(s._schedule("https://example.com/s?f[1]=b&f[0]=a"))[0]
        assert "f%5B1%5D=b" in req.url or "f[1]=b" in req.url

    def test_identity_params_unaffected_by_cap(self):
        """The user-facing risk: a product catalog using query params must crawl fully."""
        s = self._spider()
        for i in range(5):
            reqs = list(s._schedule(f"https://example.com/p?id={i}&color=red&size=l&fit=slim"))
            assert len(reqs) == 1, "identity params must never trip the facet cap"

    def test_max_facet_depth_override(self):
        s = self._spider(max_facet_depth=4)
        assert len(list(s._schedule("https://example.com/s?f[0]=a&f[1]=b&f[2]=c"))) == 1

    def test_bad_max_facet_depth_falls_back_to_default(self):
        s = self._spider(max_facet_depth="nonsense")
        assert s.max_facet_depth == WebsiteSpider.MAX_FACET_DEPTH


class TestRobotsDisallow:
    """robots.txt Disallow obedience (issues #57/#59): once robots.txt is parsed, its
    Disallow rules gate scheduling; a site with none is allow-all (behavior unchanged)."""

    def _spider(self, robots_body=None):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        if robots_body is not None:
            from protego import Protego
            s._robots = Protego.parse(robots_body)
        return s

    def test_is_robots_disallowed_matches_and_allows(self):
        s = self._spider("User-agent: *\nDisallow: /search/\nAllow: /search/help\n")
        assert s.is_robots_disallowed("https://example.com/search/?f[0]=a")
        assert not s.is_robots_disallowed("https://example.com/about")
        assert not s.is_robots_disallowed("https://example.com/search/help")  # Allow wins

    def test_no_robots_is_allow_all(self):
        s = self._spider()  # no robots.txt parsed
        assert s._robots is None
        assert not s.is_robots_disallowed("https://example.com/search/anything")

    def test_named_yoko_group_is_honored(self):
        s = self._spider("User-agent: yoko-crawler\nDisallow: /private/\n")
        assert s.is_robots_disallowed("https://example.com/private/x")

    def test_disallowed_url_not_scheduled(self):
        s = self._spider("User-agent: *\nDisallow: /search/\n")
        reqs = list(s._schedule("https://example.com/search/results"))
        assert reqs == []
        assert s.crawler.stats.values.get("robots_disallowed_skipped") == 1

    def test_allowed_url_is_scheduled(self):
        s = self._spider("User-agent: *\nDisallow: /search/\n")
        reqs = list(s._schedule("https://example.com/about"))
        assert len(reqs) == 1

    def test_parse_robots_populates_rules(self):
        s = self._spider()
        body = b"User-agent: *\nDisallow: /search/\n"
        resp = TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"), status=200)
        list(s.parse_robots(resp))  # consume the generator
        assert s._robots is not None
        assert s.is_robots_disallowed("https://example.com/search/x")


class TestRobotsCrawlDelay:
    """robots.txt Crawl-delay honoring (issue #57): max(our floor, min(asked, cap))."""

    def test_honored_crawl_delay_math(self):
        assert WebsiteSpider._honored_crawl_delay(10.0, 1.0, 10.0) == (10.0, False)
        assert WebsiteSpider._honored_crawl_delay(30.0, 1.0, 10.0) == (10.0, True)  # clamped
        assert WebsiteSpider._honored_crawl_delay(0.5, 1.0, 10.0) == (1.0, False)  # below floor
        assert WebsiteSpider._honored_crawl_delay(5.0, 1.0, 10.0) == (5.0, False)

    def _crawler(self, download_delay, throttle=None, slot=None):
        exts = types.SimpleNamespace(middlewares=[throttle] if throttle is not None else [])
        downloader = types.SimpleNamespace(slots={"example.com": slot} if slot is not None else {})
        return types.SimpleNamespace(
            stats=_FakeStats(),
            settings=types.SimpleNamespace(getfloat=lambda _k: download_delay),
            extensions=exts,
            engine=types.SimpleNamespace(downloader=downloader),
        )

    def test_apply_raises_slot_and_autothrottle_floor(self):
        s = WebsiteSpider(domain="example.com")
        throttle = types.SimpleNamespace(mindelay=1.0)  # duck-types as AutoThrottle
        slot = types.SimpleNamespace(delay=1.0)
        s.crawler = self._crawler(1.0, throttle=throttle, slot=slot)
        s._apply_crawl_delay(10.0)
        assert slot.delay == 10.0
        assert throttle.mindelay == 10.0
        assert s.crawler.stats.values.get("robots_crawl_delay_applied") == 1
        # Issue #74: the SECONDS matter, not just that it happened -- "we paced at 10s
        # because the site asked 10s" is what an operator needs to read a partial crawl.
        assert s.crawler.stats.values.get("robots_crawl_delay_honored") == 10.0
        assert s.crawler.stats.values.get("robots_crawl_delay_requested") == 10.0

    def test_apply_clamps_to_cap(self):
        s = WebsiteSpider(domain="example.com")
        s.max_robots_crawl_delay = 10.0
        slot = types.SimpleNamespace(delay=1.0)
        s.crawler = self._crawler(1.0, slot=slot)
        s._apply_crawl_delay(3600.0)  # pathological
        assert slot.delay == 10.0  # clamped to the cap, not 3600
        # The clamp is the case an operator most needs surfaced (issue #74): the crawl will
        # finalize partial, and honored-vs-requested is the only record of why.
        assert s.crawler.stats.values.get("robots_crawl_delay_honored") == 10.0
        assert s.crawler.stats.values.get("robots_crawl_delay_requested") == 3600.0

    def test_apply_noop_when_asked_below_our_floor(self):
        s = WebsiteSpider(domain="example.com")
        slot = types.SimpleNamespace(delay=3.0)
        s.crawler = self._crawler(3.0, slot=slot)  # our floor 3s
        s._apply_crawl_delay(1.0)  # site asks for less than we already pace
        assert slot.delay == 3.0  # unchanged
        assert s.crawler.stats.values.get("robots_crawl_delay_applied") is None


class TestSkippedAuthUrls:
    """issue #43: a deliberately-skipped auth/login-gated URL emits a `skip_reason` record
    so the corpus can surface it as coverage, without ever counting as a crawled page."""

    def _spider(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_login_url_emits_skip_row(self):
        s = self._spider()
        out = list(s._schedule("https://example.com/account/login",
                               referrer_emit="https://example.com/"))
        assert not any(isinstance(o, Request) for o in out)  # not fetched
        rows = [o for o in out if isinstance(o, dict)]
        assert len(rows) == 1
        r = rows[0]
        assert r["skip_reason"] == "login_gated"
        assert r["status"] == 0
        assert r["url"].endswith("/account/login")
        assert r["referrer"] == "https://example.com/"
        assert s.crawler.stats.values.get("login_urls_skipped") == 1

    def test_non_login_url_has_no_skip_row(self):
        s = self._spider()
        out = list(s._schedule("https://example.com/about"))
        assert any(isinstance(o, Request) for o in out)
        assert not any(isinstance(o, dict) and o.get("skip_reason") for o in out)

    def test_skip_row_emitted_once_per_url(self):
        s = self._spider()
        first = list(s._schedule("https://example.com/login"))
        second = list(s._schedule("https://example.com/login"))
        assert len([o for o in first if isinstance(o, dict)]) == 1
        assert second == []  # seen -> no duplicate skip row

    def test_fetched_page_row_carries_empty_skip_reason(self):
        spider = WebsiteSpider(domain="example.com")
        row = _emit_one(spider, _html_response())
        assert row["skip_reason"] == ""


class TestResumableDedupState:
    """issue #52: dedup state must survive between resumable crawler sessions.

    These tests drive a REAL `scrapy.crawler.Crawler` in the same order production does --
    `_create_spider()` then `_apply_settings()` -- because that ordering is the whole
    difficulty. An earlier cut of this feature bound the state from a `spider_opened`
    handler; since the spider (and its handler) are created BEFORE extensions load,
    SpiderState's own handler registered second and ran second, so the bind saw no `state`
    and silently did nothing. Every hand-rolled unit test still passed. Only a test that
    exercises the real wiring catches that, so these do.
    """

    @staticmethod
    def _install_reactor():
        """Crawler._apply_settings() requires an installed reactor."""
        from scrapy.utils.reactor import install_reactor
        try:
            install_reactor("twisted.internet.asyncioreactor.AsyncioSelectorReactor")
        except Exception:
            pass  # already installed by an earlier test in this process

    def _session(self, jobdir, urls=()):
        """Run one crawler session against `jobdir`, in the real Crawler.crawl() order."""
        from scrapy.crawler import Crawler
        from scrapy import signals
        self._install_reactor()
        crawler = Crawler(WebsiteSpider, settings={"JOBDIR": jobdir, "LOG_ENABLED": False})
        spider = crawler._create_spider(domain="example.com")
        crawler.spider = spider
        crawler._apply_settings()
        crawler.signals.send_catch_log(signals.spider_opened, spider=spider)
        fetched = sum(1 for u in urls if list(spider._schedule(u)))
        crawler.signals.send_catch_log(signals.spider_closed, spider=spider, reason="finished")
        return spider, fetched

    def test_state_is_bound_through_real_scrapy_wiring(self, tmp_path):
        """The regression guard: `seen` must BE the object inside `spider.state`, after a
        real Crawler set things up. If this is a copy (or state is empty) nothing persists."""
        spider, _ = self._session(str(tmp_path), ["https://example.com/a"])
        assert spider.seen is spider.state["seen"]
        assert spider.emitted is spider.state["emitted"]
        assert spider.first_referrer is spider.state["first_referrer"]

    def test_state_is_written_to_jobdir(self, tmp_path):
        self._session(str(tmp_path), ["https://example.com/a"])
        assert (tmp_path / "spider.state").exists()

    def test_resumed_session_does_not_refetch(self, tmp_path):
        urls = [f"https://example.com/p{i}" for i in range(5)]
        _, first = self._session(str(tmp_path), urls)
        assert first == 5
        spider, second = self._session(str(tmp_path), urls)
        assert second == 0, "a resumed session must not re-fetch earlier sessions' pages"
        assert len(spider.seen) == 5, "dedup state should have been carried over"

    def test_resumed_session_still_crawls_new_pages(self, tmp_path):
        """Resume must not freeze the crawl -- newly discovered URLs still schedule."""
        self._session(str(tmp_path), ["https://example.com/old"])
        _, fetched = self._session(str(tmp_path), ["https://example.com/new"])
        assert fetched == 1

    def test_without_jobdir_behaviour_is_unchanged(self):
        """No JOBDIR -> SpiderState does not attach -> the plain in-memory sets, as before."""
        from scrapy.crawler import Crawler
        from scrapy import signals
        self._install_reactor()
        crawler = Crawler(WebsiteSpider, settings={"LOG_ENABLED": False})
        spider = crawler._create_spider(domain="example.com")
        crawler.spider = spider
        crawler._apply_settings()
        crawler.signals.send_catch_log(signals.spider_opened, spider=spider)
        urls = [f"https://example.com/p{i}" for i in range(5)]
        assert sum(1 for u in urls if list(spider._schedule(u))) == 5

    def test_corrupt_state_is_discarded_not_fatal(self):
        """A hand-edited or version-skewed JOBDIR must not crash a multi-hour crawl."""
        import types
        spider = WebsiteSpider(domain="example.com")
        spider.crawler = types.SimpleNamespace(stats=_FakeStats())
        spider.state = {"seen": ["not", "a", "set"], "emitted": None, "first_referrer": 7}
        spider._bind_dedup_state()
        assert isinstance(spider.seen, set) and not spider.seen
        assert isinstance(spider.emitted, set)
        assert isinstance(spider.first_referrer, dict)
        assert list(spider._schedule("https://example.com/a"))  # still crawls

    def test_bind_is_idempotent(self):
        """First-use binding runs on every _schedule call; it must not reset state."""
        import types
        spider = WebsiteSpider(domain="example.com")
        spider.crawler = types.SimpleNamespace(stats=_FakeStats())
        spider.state = {}
        list(spider._schedule("https://example.com/a"))
        list(spider._schedule("https://example.com/b"))
        assert len(spider.seen) == 2 and spider.seen is spider.state["seen"]


class TestBreadthFirstOrdering:
    """issue #52: Scrapy defaults to a LIFO queue (depth-first) with no depth limit, which
    turns an infinitely-branching subtree into a trapdoor -- on naeyc.org the crawl fetched
    zero real pages after entering a faceted-search subtree at row 430."""

    def _settings(self, **over):
        import argparse, run_spider
        args = argparse.Namespace(output="o.jsonl", format="jsonlines", emit_content=False,
                                  user_agent=None, delay=1.0, profile="presale",
                                  status_file="s.json", impersonate="off", jobdir=None, proxy=None)
        for k, v in over.items():
            setattr(args, k, v)
        return run_spider.build_settings(args)

    def test_scheduler_is_breadth_first(self):
        s = self._settings()
        assert s["DEPTH_PRIORITY"] == 1
        assert s["SCHEDULER_MEMORY_QUEUE"].endswith("FifoMemoryQueue")
        assert s["SCHEDULER_DISK_QUEUE"].endswith("PickleFifoDiskQueue")

    def test_breadth_first_applies_to_every_profile(self):
        """A trap is not a politeness question -- BFO must hold for standard crawls too."""
        s = self._settings(profile="standard")
        assert s["DEPTH_PRIORITY"] == 1
        assert s["SCHEDULER_MEMORY_QUEUE"].endswith("FifoMemoryQueue")


class TestSeedRequests:
    """issue #52 review: the crawl's seeds must actually be seeded.

    Scrapy 2.13 replaced `start_requests()` with `async def start()`, and 2.17 removed the
    base method and every call site. `requirements.txt` had an unbounded `scrapy>=2.11`, so
    an ordinary upgrade silently made our `start_requests` dead code -- the default
    `Spider.start()` seeded start_urls with no robots.txt. Nothing failed. The dead path was
    sitemap discovery: without robots.txt the crawler was link-following only."""

    def _seeds(self, **kw):
        import asyncio
        from scrapy.utils.reactor import install_reactor
        try:
            install_reactor("twisted.internet.asyncioreactor.AsyncioSelectorReactor")
        except Exception:
            pass
        from scrapy.crawler import Crawler
        crawler = Crawler(WebsiteSpider, settings={"LOG_ENABLED": False})
        spider = crawler._create_spider(domain="example.com", **kw)
        crawler.spider = spider
        crawler._apply_settings()

        async def collect():
            return [r async for r in spider.start()]
        return asyncio.new_event_loop().run_until_complete(collect())

    def test_start_seeds_only_robots_txt(self):
        """The start URL is NOT a seed any more (issue #76): it is emitted from
        parse_robots, so the Disallow gate is always in its final state before any page URL
        is scheduled. Seeding it here too is what let the first hop escape the gate."""
        urls = [r.url for r in self._seeds()]
        assert urls == ["https://example.com/robots.txt"], urls

    def test_robots_seed_carries_an_errback(self):
        """Without an errback a DNS/connection failure on robots.txt would never reach a
        callback, the start URLs would never be emitted, and the crawl would fetch nothing
        (issue #76). The errback is what keeps a network blip from zeroing a crawl."""
        seeds = self._seeds()
        assert all(r.errback is not None for r in seeds)

    def test_start_seeds_robots_txt(self):
        """robots.txt is how sitemaps are discovered -- without it the crawler is
        link-following only and never sees sitemap-only or orphaned pages."""
        assert "https://example.com/robots.txt" in [r.url for r in self._seeds()]

    def test_seeds_carry_no_cookies(self):
        """Cookie injection was removed: seeds must not carry any request cookies (the site's
        own Set-Cookie still populates the jar during the crawl via COOKIES_ENABLED)."""
        seeds = self._seeds()
        assert seeds, "no seed requests generated"
        assert all(not r.cookies for r in seeds)

    def test_start_and_start_requests_agree(self):
        """Both entry points must produce the same seeds, so a Scrapy version change can
        never silently alter what gets crawled."""
        import asyncio
        from scrapy.utils.reactor import install_reactor
        try:
            install_reactor("twisted.internet.asyncioreactor.AsyncioSelectorReactor")
        except Exception:
            pass
        from scrapy.crawler import Crawler
        crawler = Crawler(WebsiteSpider, settings={"LOG_ENABLED": False})
        spider = crawler._create_spider(domain="example.com")
        crawler.spider = spider
        crawler._apply_settings()

        async def collect():
            return [r.url async for r in spider.start()]
        via_start = asyncio.new_event_loop().run_until_complete(collect())
        via_legacy = [r.url for r in spider.start_requests()]
        assert via_start == via_legacy


class TestJobdirFormatMigration:
    """issue #52 review: breadth-first swapped SCHEDULER_DISK_QUEUE from Scrapy's default
    PickleLifoDiskQueue to PickleFifoDiskQueue, whose on-disk layouts are incompatible.
    Every paused resumable crawl in flight at rollout would die in Scheduler.open(), and
    because that session closes as `shutdown` job_manager KEEPS the jobdir -- so the crash
    repeats forever and the domain is permanently bricked.

    The subtlety these tests pin: `Scheduler._dqdir` mkdirs `requests.queue/` under BOTH
    formats, so checking that path detects nothing. It is each per-priority SLOT inside it
    that differs -- a FILE under Lifo, a DIRECTORY under Fifo. An earlier cut of the guard
    tested the wrong level and never fired.
    """

    FIFO = "scrapy.squeues.PickleFifoDiskQueue"
    LIFO = "scrapy.squeues.PickleLifoDiskQueue"

    def _jobdir(self, tmp_path, slot_is_dir):
        qdir = tmp_path / "requests.queue"
        qdir.mkdir()
        (qdir / "active.json").write_text("[]")          # Scheduler bookkeeping
        if slot_is_dir:
            (qdir / "0").mkdir()
            (qdir / "0" / "q00000").write_bytes(b"chunk")  # Fifo layout
        else:
            (qdir / "0").write_bytes(b"lifo single file")  # Lifo layout
        (tmp_path / "spider.state").write_bytes(b"state")
        return str(tmp_path)

    def test_old_lifo_jobdir_is_reset(self, tmp_path):
        from run_spider import reset_incompatible_jobdir
        jobdir = self._jobdir(tmp_path, slot_is_dir=False)
        assert reset_incompatible_jobdir(jobdir, disk_queue=self.FIFO) is True
        assert not tmp_path.exists()

    def test_matching_fifo_jobdir_is_kept(self, tmp_path):
        """Must NOT wipe every session -- that would restart every long crawl forever."""
        from run_spider import reset_incompatible_jobdir
        jobdir = self._jobdir(tmp_path, slot_is_dir=True)
        assert reset_incompatible_jobdir(jobdir, disk_queue=self.FIFO) is False
        assert (tmp_path / "spider.state").exists()

    def test_rollback_is_also_self_healing(self, tmp_path):
        """Reverting to the Lifo queue must reset a Fifo-format JOBDIR too -- crossing the
        formats raises IsADirectoryError in that direction, equally fatal."""
        from run_spider import reset_incompatible_jobdir
        jobdir = self._jobdir(tmp_path, slot_is_dir=True)
        assert reset_incompatible_jobdir(jobdir, disk_queue=self.LIFO) is True

    def test_requests_queue_dir_alone_is_not_the_discriminator(self, tmp_path):
        """Pins the bug an earlier cut had: `requests.queue` is a DIRECTORY under both
        formats, so a guard keyed on it never fires."""
        import os
        lifo = self._jobdir(tmp_path / "a", slot_is_dir=False) if False else None
        for slot_is_dir in (True, False):
            d = tmp_path / f"jd{int(slot_is_dir)}"
            d.mkdir()
            self._jobdir(d, slot_is_dir=slot_is_dir)
            assert os.path.isdir(os.path.join(str(d), "requests.queue"))

    def test_empty_or_missing_jobdir_is_untouched(self, tmp_path):
        from run_spider import reset_incompatible_jobdir
        assert reset_incompatible_jobdir(str(tmp_path / "nope"), disk_queue=self.FIFO) is False
        (tmp_path / "requests.queue").mkdir()
        assert reset_incompatible_jobdir(str(tmp_path), disk_queue=self.FIFO) is False

    def test_real_queuelib_slot_formats_are_incompatible(self, tmp_path):
        """Pins the premise against the installed queuelib, in BOTH directions."""
        import pytest
        from queuelib import FifoDiskQueue, LifoDiskQueue
        lifo_slot = str(tmp_path / "lifo")
        q = LifoDiskQueue(lifo_slot); q.push(b"x"); q.close()
        with pytest.raises(NotADirectoryError):
            FifoDiskQueue(lifo_slot)
        fifo_slot = str(tmp_path / "fifo")
        q = FifoDiskQueue(fifo_slot); q.push(b"x"); q.close()
        with pytest.raises(IsADirectoryError):
            LifoDiskQueue(fifo_slot)

    def test_build_settings_resets_before_handing_jobdir_to_scrapy(self, tmp_path):
        """The migration must run on the real code path, with the configured queue class."""
        import argparse
        from run_spider import build_settings
        jobdir = self._jobdir(tmp_path, slot_is_dir=False)
        args = argparse.Namespace(output="o.jsonl", format="jsonlines", emit_content=False,
                                  user_agent=None, delay=1.0, profile="presale",
                                  status_file="s.json", impersonate="off",
                                  jobdir=jobdir, proxy=None)
        settings = build_settings(args)
        assert settings["JOBDIR"] == jobdir
        assert not (tmp_path / "requests.queue").exists()


class TestPaginationFollowing:
    """Follow real pagination (?page=N) as a discovery path, but never chase reorderings
    (?sort/order/dir) or record the pagination URLs themselves (issue #58)."""

    @staticmethod
    def _scheduled_urls(spider, url, **kw):
        import scrapy
        return [r.url for r in spider._schedule(url, **kw) if isinstance(r, scrapy.Request)]

    def test_reach_pagination_follows_page_param(self):
        # The production path runs with reach_pagination=1; a ?page=N link must be enqueued
        # with the page param intact so the Nth listing page is actually fetched.
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        urls = self._scheduled_urls(
            sp, "https://example.com/resources/blog?page=2",
            referrer_emit="https://example.com/resources/blog",
        )
        assert urls == ["https://example.com/resources/blog?page=2"]

    def test_reach_pagination_visits_each_page_as_a_distinct_target(self):
        # page=1 and page=2 are DIFFERENT sets of items -> two distinct scheduled requests;
        # re-scheduling the same page is deduped to nothing.
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        first = self._scheduled_urls(sp, "https://example.com/blog?page=1")
        second = self._scheduled_urls(sp, "https://example.com/blog?page=2")
        again = self._scheduled_urls(sp, "https://example.com/blog?page=2")
        assert first == ["https://example.com/blog?page=1"]
        assert second == ["https://example.com/blog?page=2"]
        assert again == []  # already seen

    def test_default_mode_does_not_follow_pagination(self):
        # Without reach_pagination the page param is stripped, so ?page=2 collapses onto the
        # base listing (the original, conservative behavior).
        sp = WebsiteSpider(domain="example.com")
        urls = self._scheduled_urls(sp, "https://example.com/blog?page=2")
        assert urls == ["https://example.com/blog"]

    def test_reorder_params_are_never_followed(self):
        # sort/order/dir only re-sort the SAME items. Even with reach_pagination on they are
        # stripped in schedule-mode, so a sort variant collapses onto the base listing
        # (the fix for the page x sort x order permutation explosion, issue #58).
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        assert sp.normalize_url(
            "https://example.com/blog?sort=title&order=desc&dir=asc",
            exclude_params=sp.exclude_params_schedule,
        ) == "https://example.com/blog"

    def test_drupal_views_exposed_sort_params_are_stripped(self):
        # Real-site (naeyc.org) reordering: Drupal Views exposes sort as ?sort_by=&sort_order=,
        # not the short generic names. These must strip in every mode too, or a sorted listing
        # leaks as a duplicate page (issue #58, found in the live crawl).
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        url = "https://example.com/resources/pubs/books/all?sort_by=field_publication_date&sort_order=DESC"
        assert sp.normalize_url(url, exclude_params=sp.exclude_params_schedule) == "https://example.com/resources/pubs/books/all"
        assert sp.normalize_url(url, exclude_params=sp.exclude_params_emit) == "https://example.com/resources/pubs/books/all"
        # A page param alongside an exposed sort still keeps the page, drops the sort.
        assert sp.normalize_url(
            "https://example.com/list?page=2&sort_by=title&sort_bef_combine=asc",
            exclude_params=sp.exclude_params_schedule,
        ) == "https://example.com/list?page=2"

    def test_page_kept_but_sort_dropped_when_combined(self):
        # A ?page=3&sort=title link keeps the sequence param (real next page) and drops the
        # reordering param, so pagination is followed without fanning out per sort order.
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        assert sp.normalize_url(
            "https://example.com/blog?page=3&sort=title",
            exclude_params=sp.exclude_params_schedule,
        ) == "https://example.com/blog?page=3"

    def test_pagination_stripped_on_emit(self):
        # The pagination URLs are a discovery path, not content: the emitted/stored row for
        # a ?page=N page normalizes to the canonical listing, so ?page= never appears as a
        # distinct recorded page.
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        assert sp.normalize_url(
            "https://example.com/blog?page=2",
            exclude_params=sp.exclude_params_emit,
        ) == "https://example.com/blog"

    def test_offset_start_p_are_followed_like_page(self):
        # page is the heavily-exercised case; the other sequence params must behave the same
        # (kept in schedule, revealing new items) so the fix isn't page-only.
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        for param in ("offset=20", "start=40", "p=123"):
            url = f"https://example.com/blog?{param}"
            assert self._scheduled_urls(sp, url) == [url], param

    def test_pager_links_are_followed_end_to_end(self):
        # The real issue-#58 scenario, driven through parse(): a listing page's Drupal pager
        # <a href="?page=N"> links are extracted AND scheduled (page retained), a normal post
        # link is followed, and a ?sort/?order reordering never produces a sort= request.
        import types
        from scrapy.http import Request
        sp = WebsiteSpider(domain="example.com", reach_pagination=1)
        sp.crawler = types.SimpleNamespace(stats=_FakeStats())
        body = (
            b"<html><body>"
            b"<a href='/resources/blog?page=1'>2</a>"
            b"<a href='/resources/blog?page=2'>3</a>"
            b"<a href='/resources/blog?sort=title&order=desc'>Sort by title</a>"
            b"<a href='/resources/blog/a-real-post'>A real post</a>"
            b"</body></html>"
        )
        resp = _html_response(body=body, url="https://example.com/resources/blog")
        scheduled = [o.url for o in sp.parse(resp) if isinstance(o, Request)]
        assert "https://example.com/resources/blog?page=1" in scheduled
        assert "https://example.com/resources/blog?page=2" in scheduled
        assert "https://example.com/resources/blog/a-real-post" in scheduled
        # No reordering is ever fetched as a distinct URL (it collapses to the base listing).
        assert not any("sort=" in u or "order=" in u or "dir=" in u for u in scheduled)

    def test_sequence_and_reorder_partition_covers_pagination_params(self):
        # Guard the split: every historical pagination param is classified as exactly one of
        # sequence (followed) or reorder (stripped), and the two are disjoint.
        assert WebsiteSpider.SEQUENCE_PARAMS.isdisjoint(WebsiteSpider.REORDER_PARAMS)
        assert WebsiteSpider.SEQUENCE_PARAMS | WebsiteSpider.REORDER_PARAMS == WebsiteSpider.PAGINATION_PARAMS
        # BOTH classes must live in UNWANTED so the default (no reach/keep) mode strips them;
        # the modes then selectively re-admit only SEQUENCE. A sequence param that fell out of
        # UNWANTED would be kept even in default mode (silent behavior drift).
        assert WebsiteSpider.REORDER_PARAMS <= WebsiteSpider.UNWANTED_PARAMS
        assert WebsiteSpider.SEQUENCE_PARAMS <= WebsiteSpider.UNWANTED_PARAMS


class TestKeepPaginationMode:
    """keep_pagination=1 records paginated pages as distinct (schedule == emit). This PR
    changed that branch to subtract SEQUENCE_PARAMS (was PAGINATION_PARAMS), so reorderings
    are now stripped here too -- pin both halves (issue #58)."""

    def test_keep_pagination_records_page_param_in_both_modes(self):
        sp = WebsiteSpider(domain="example.com", keep_pagination=1)
        # In this mode schedule and emit share one exclude set, so a ?page=N page is both
        # visited AND recorded with the page intact (treated as a unique page).
        assert sp.exclude_params_schedule == sp.exclude_params_emit
        for mode in (sp.exclude_params_schedule, sp.exclude_params_emit):
            assert sp.normalize_url(
                "https://example.com/blog?page=2", exclude_params=mode
            ) == "https://example.com/blog?page=2"

    def test_keep_pagination_still_strips_reorderings(self):
        # Reorderings are views regardless of mode: keep_pagination keeps the sequence params
        # but must NOT resurrect sort/order/dir (the behavior this PR changed).
        sp = WebsiteSpider(domain="example.com", keep_pagination=1)
        assert sp.normalize_url(
            "https://example.com/blog?sort=title&order=desc&dir=asc",
            exclude_params=sp.exclude_params_emit,
        ) == "https://example.com/blog"


class TestRobotsSeedRace:
    """Issue #76: the Disallow gate is a no-op until robots.txt is parsed, so seeding the
    start URL alongside robots.txt let the whole first hop escape it. robots.txt is now a
    PREREQUISITE of seeding, which removes the race by construction.

    Modelled on gastro.org, where the old ordering produced 57 pages (homepage + its 56
    links) against a 2,347-URL sitemap and reported it as a complete crawl of a simple site.
    """

    GASTRO_ROBOTS = (
        b"User-agent: *\nCrawl-delay: 15\nDisallow: /\nDisallow: /wp-admin/\n"
        b"Allow: /wp-admin/admin-ajax.php\n\nUser-agent: Googlebot\nAllow: /\n"
    )

    def _spider(self, domain="gastro.org"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _resp(self, body=None, status=200, url="https://gastro.org/robots.txt", headers=None):
        return TextResponse(
            url=url, body=body if body is not None else self.GASTRO_ROBOTS,
            headers=headers or {"Content-Type": "text/plain"},
            request=Request(url), status=status,
        )

    def test_start_urls_are_gated_behind_robots(self):
        """A blanket Disallow now yields a crawl with nothing scheduled past the start URL,
        instead of the start URL plus an un-gated first hop."""
        s = self._spider()
        out = list(s.parse_robots(self._resp()))
        # The start URL is emitted (the operator asked for it) ...
        assert [r.url for r in out if isinstance(r, Request)] == ["https://gastro.org/"]
        # ... but every link discovered from it is now correctly refused.
        assert list(s._schedule("https://gastro.org/news/a-post/")) == []
        assert s.crawler.stats.values.get("robots_disallowed_skipped") == 1

    def test_rules_are_live_before_any_page_is_scheduled(self):
        """The ordering guarantee: by the time the start URL exists, _robots is populated."""
        s = self._spider()
        assert s._robots is None
        list(s.parse_robots(self._resp()))
        assert s._robots is not None
        assert s.is_robots_disallowed("https://gastro.org/anything/")

    def test_start_urls_emitted_exactly_once_across_a_redirect_chain(self):
        """robots.txt redirecting re-enters parse_robots; the start URL must not double."""
        s = self._spider()
        hop = list(s.parse_robots(self._resp(
            body=b"", status=301, headers={"Location": b"https://gastro.org/robots.txt?x=1"},
        )))
        assert [r.url for r in hop if isinstance(r, Request)] == ["https://gastro.org/robots.txt?x=1"]
        final = list(s.parse_robots(self._resp(url="https://gastro.org/robots.txt?x=1")))
        assert [r.url for r in final if isinstance(r, Request)] == ["https://gastro.org/"]
        # A third landing (redirect loop) must not re-seed.
        again = list(s.parse_robots(self._resp(url="https://gastro.org/robots.txt?x=1")))
        assert [r.url for r in again if isinstance(r, Request)] == []

    def test_offdomain_redirect_still_seeds(self):
        """An off-domain robots.txt redirect ends the chain -- we must still crawl the site,
        not return silently and fetch nothing."""
        s = self._spider()
        out = list(s.parse_robots(self._resp(
            body=b"", status=301, headers={"Location": b"https://elsewhere.example/robots.txt"},
        )))
        urls = [r.url for r in out if isinstance(r, Request)]
        assert "https://gastro.org/" in urls
        assert not any("elsewhere.example" in u for u in urls)

    def test_redirect_without_location_still_seeds(self):
        s = self._spider()
        out = list(s.parse_robots(self._resp(body=b"", status=301)))
        assert "https://gastro.org/" in [r.url for r in out if isinstance(r, Request)]

    def test_missing_robots_txt_seeds_allow_all(self):
        """A 404 is the common case (no robots.txt at all): crawl normally."""
        s = self._spider()
        out = list(s.parse_robots(self._resp(body=b"<html>404</html>", status=404)))
        urls = [r.url for r in out if isinstance(r, Request)]
        assert "https://gastro.org/" in urls
        # No robots.txt named a sitemap, so the conventional locations are probed (#77).
        assert urls == ["https://gastro.org" + p for p in WebsiteSpider.SITEMAP_PROBE_PATHS] \
            + ["https://gastro.org/"]
        assert s._robots is None
        assert not s.is_robots_disallowed("https://gastro.org/anything/")

    def test_non_text_robots_body_still_seeds(self):
        """A binary robots.txt has no `.text`; seeding must not die with sitemap parsing."""
        from scrapy.http import Response
        s = self._spider()
        resp = Response(url="https://gastro.org/robots.txt", body=b"\x00\x01\x02",
                        request=Request("https://gastro.org/robots.txt"), status=200)
        out = list(s.parse_robots(resp))
        assert "https://gastro.org/" in [r.url for r in out if isinstance(r, Request)]

    def test_transport_failure_seeds_allow_all(self):
        """Issue #76's regression guard: a DNS/connection failure on robots.txt must not
        produce a zero-page crawl."""
        s = self._spider()
        failure = types.SimpleNamespace(value=OSError("dns failure"))
        out = list(s.robots_failed(failure))
        assert "https://gastro.org/" in [r.url for r in out]
        assert s.crawler.stats.values.get("seeding/robots_failed") == 1
        assert not s.is_robots_disallowed("https://gastro.org/anything/")

    def test_seeds_emitted_tripwire_still_counts_start_urls(self):
        """`seeding/seeds_emitted` is the #52 tripwire -- it must still count the pages we
        seed, not just the robots.txt request."""
        s = self._spider()
        list(s.parse_robots(self._resp(body=b"User-agent: *\nAllow: /\n")))
        assert s.crawler.stats.values.get("seeding/seeds_emitted") == 1

    def test_permissive_robots_is_unchanged(self):
        """The common case must behave exactly as before: seed, then crawl freely."""
        s = self._spider()
        out = list(s.parse_robots(self._resp(
            body=b"User-agent: *\nDisallow: /search/\nSitemap: https://gastro.org/sitemap.xml\n"
        )))
        urls = [r.url for r in out if isinstance(r, Request)]
        assert "https://gastro.org/sitemap.xml" in urls
        assert "https://gastro.org/" in urls
        assert len(list(s._schedule("https://gastro.org/news/a-post/"))) == 1
        assert list(s._schedule("https://gastro.org/search/x")) == []


class TestRobotsSeedRaceHardening:
    """Findings from the #76 code review: the paths that turn the fix itself into a
    zero-page crawl. Each of these was reproducible on the first cut of the change."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _redirect(self, url, location, hops=None):
        req = Request(url)
        if hops is not None:
            req.meta["robots_hops"] = hops
        return TextResponse(
            url=url, body=b"", status=301,
            headers={"Location": location.encode()}, request=req,
        )

    def test_self_referential_redirect_loop_terminates_and_seeds(self):
        """A robots.txt that 301s to ITSELF (the classic apex/www or http/https page-rule
        bug). REDIRECT_ENABLED is False and the hop carries dont_filter, so nothing else
        bounds this -- unbounded, it spins until CLOSESPIDER_TIMEOUT and emits one row while
        reporting `completed`."""
        s = self._spider()
        url = "https://example.com/robots.txt"
        hops, seeded = 0, []
        resp = self._redirect(url, url)
        for _ in range(20):
            out = [r for r in s.parse_robots(resp) if isinstance(r, Request)]
            if not out:
                break
            nxt = out[0]
            if nxt.url != url:      # the chain resolved -> start URL + sitemap probes
                seeded.extend(r.url for r in out if r.url == "https://example.com/")
                break
            hops += 1
            resp = self._redirect(url, url, hops=nxt.meta.get("robots_hops"))
        assert hops <= WebsiteSpider.MAX_ROBOTS_REDIRECTS, f"unbounded after {hops} hops"
        assert seeded == ["https://example.com/"], "loop terminated without seeding"

    def test_ping_pong_redirect_terminates_and_seeds(self):
        """A -> B -> A, the other shape of the same misconfiguration."""
        s = self._spider()
        a, b = "https://example.com/robots.txt", "https://www.example.com/robots.txt"
        cur, nxt_url, seeded, hops = a, b, [], 0
        meta_hops = None
        for _ in range(20):
            out = [r for r in s.parse_robots(self._redirect(cur, nxt_url, hops=meta_hops))
                   if isinstance(r, Request)]
            if not out:
                break
            r = out[0]
            if not r.url.endswith("robots.txt"):
                seeded.extend(x.url for x in out if x.url == "https://example.com/")
                break
            hops += 1
            meta_hops = r.meta.get("robots_hops")
            cur, nxt_url = nxt_url, cur
        assert hops <= WebsiteSpider.MAX_ROBOTS_REDIRECTS
        assert seeded == ["https://example.com/"]

    def test_malformed_sitemap_url_does_not_cost_the_seed(self):
        """A fullwidth solidus in a `Sitemap:` line (a real CMS copy-paste artifact) makes
        urlparse raise. Before the fix that exception escaped and skipped seeding, so the
        crawl reported `completed` with one row."""
        s = self._spider()
        body = ("User-agent: *\nAllow: /\n"
                "Sitemap: http://a／b.example.com/sitemap.xml\n").encode()
        resp = TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"), status=200)
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert "https://example.com/" in urls
        assert s._robots is not None, "the rules must still have been parsed"

    def test_malformed_location_header_does_not_cost_the_seed(self):
        s = self._spider()
        resp = self._redirect("https://example.com/robots.txt", "http://[::1")
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert "https://example.com/" in urls

    def test_start_url_carries_no_referrer(self):
        """The site root has no referrer. Yielding it from a callback would otherwise let
        RefererMiddleware stamp /robots.txt on it, and _emit_row's header fallback would
        report the root as linked-from-robots.txt -- a value corpus persists and exports."""
        s = self._spider()
        body = b"User-agent: *\nAllow: /\n"
        resp = TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"), status=200)
        start = [r for r in s.parse_robots(resp)
                 if isinstance(r, Request) and r.url == "https://example.com/"]
        assert len(start) == 1
        assert start[0].headers.get("Referer") is None

    def test_robots_seed_and_start_url_carry_dont_filter(self):
        """Load-bearing for a JOBDIR resume: the dupefilter persists across sessions, so
        without dont_filter session 2 filters the robots seed, never runs parse_robots, and
        therefore never emits the start URL -- a zero-page resume."""
        s = self._spider()
        seeds = list(s._seed_requests())
        assert all(r.dont_filter for r in seeds)
        body = b"User-agent: *\nAllow: /\n"
        resp = TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"), status=200)
        starts = [r for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert all(r.dont_filter for r in starts)

    def test_robots_rules_survive_a_resumed_session(self):
        """The gate must be CLOSED before the first restored frontier page is parsed on a
        resume, or #76 survives on the multi-session path yoko-corpus actually uses."""
        s1 = self._spider("gastro.org")
        s1.state = {}
        body = b"User-agent: *\nDisallow: /\n"
        resp = TextResponse(url="https://gastro.org/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://gastro.org/robots.txt"), status=200)
        list(s1.parse_robots(resp))
        assert isinstance(s1.state.get("robots_body"), str)

        # Session 2: same JOBDIR state, no robots.txt response yet.
        s2 = self._spider("gastro.org")
        s2.state = s1.state
        assert s2._robots is None
        assert list(s2._schedule("https://gastro.org/news/a-post/")) == []
        assert s2._robots is not None, "rules must be restored on first schedule"
        assert s2.crawler.stats.values.get("robots_disallowed_skipped") == 1

    def test_no_jobdir_resume_is_unaffected(self):
        """With no JOBDIR there is no state; persistence must be a silent no-op."""
        s = self._spider()
        body = b"User-agent: *\nDisallow: /private/\n"
        resp = TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"), status=200)
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert "https://example.com/" in urls
        assert s.is_robots_disallowed("https://example.com/private/x")


class TestPlatformSignals:
    """corpus #112: the crawler records what the server says about its CMS, because the URL
    space cannot see a well-configured WordPress site. Review found this half had NO tests
    and it is where the capture-point and selector bugs lived."""

    def _spider(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _page(self, body=b"<html><head></head><body>hi</body></html>", headers=None,
              status=200, url="https://example.com/"):
        h = {"Content-Type": "text/html; charset=utf-8"}
        h.update(headers or {})
        return TextResponse(url=url, body=body, headers=h,
                            request=Request(url), status=status)

    def _signals(self, spider):
        return spider.crawler.stats.values.get("platform_signals")

    def test_wp_rest_link_header_is_recorded(self):
        s = self._spider()
        s._record_platform_signals(self._page(headers={
            "Link": '<https://example.com/wp-json/>; rel="https://api.w.org/"'}))
        assert self._signals(s)["wp-rest-api-link"]

    def test_wp_rest_link_tag_is_recorded(self):
        s = self._spider()
        s._record_platform_signals(self._page(
            body=b'<html><head><link rel="https://api.w.org/" href="/wp-json/"></head></html>'))
        assert self._signals(s)["wp-rest-api-link"] == "https://api.w.org/"

    def test_rel_is_matched_as_a_token_list(self):
        """`rel` is space-separated; `rel="https://api.w.org/ alternate"` is valid markup."""
        s = self._spider()
        s._record_platform_signals(self._page(
            body=b'<html><head><link rel="https://api.w.org/ alternate" href="/wp-json/">'
                 b'</head></html>'))
        assert self._signals(s)["wp-rest-api-link"] == "https://api.w.org/"

    def test_every_generator_meta_is_captured(self):
        """Plugins APPEND a generator; they do not overwrite WordPress core's. Taking only
        the first meant that on a site stripping `wp_generator` we kept whichever plugin was
        first and threw the rest away -- so identifying the platform came down to plugin
        load order (corpus #115)."""
        s = self._spider()
        s._record_platform_signals(self._page(
            body=b'<html><head>'
                 b'<meta name="generator" content="WordPress 6.9.4">'
                 b'<meta name="generator" content="Elementor 4.2.3">'
                 b'<meta name="generator" content="WooCommerce 8.4.0">'
                 b'</head></html>'))
        gen = self._signals(s)["generator"]
        assert "WordPress 6.9.4" in gen
        assert "Elementor 4.2.3" in gen
        assert "WooCommerce 8.4.0" in gen

    def test_generator_capture_is_bounded(self):
        s = self._spider()
        metas = b"".join(
            b'<meta name="generator" content="Plugin %d">' % i for i in range(20))
        s._record_platform_signals(self._page(body=b"<html><head>" + metas + b"</head></html>"))
        gen = self._signals(s)["generator"]
        assert gen.count(";") == WebsiteSpider._PLATFORM_MAX_GENERATORS - 1

    def test_generator_is_matched_case_insensitively(self):
        """Drupal core emits `name="Generator"` with a capital G, and CSS attribute-VALUE
        matching is case-sensitive — a [name="generator"] selector is blind to it."""
        s = self._spider()
        s._record_platform_signals(self._page(
            body=b'<html><head><meta name="Generator" content="Drupal 10"></head></html>'))
        assert self._signals(s)["generator"] == "Drupal 10"

    def test_a_bare_x_powered_by_does_not_latch(self):
        """THE BUG: latching on a non-identifying header stops us looking. A WordPress site
        whose homepage is edge-cached (REST link and generator stripped) carries both on its
        interior pages — latching on `PHP/8.2` leaves it reported as "Custom / other"."""
        s = self._spider()
        s._record_platform_signals(self._page(headers={"X-Powered-By": "PHP/8.2"}))
        assert self._signals(s) is None
        assert s._platform_recorded is False
        # ...and the next page, which does identify the CMS, is still inspected.
        s._record_platform_signals(self._page(
            body=b'<html><head><meta name="generator" content="WordPress 6.9.4"></head></html>',
            url="https://example.com/about"))
        assert self._signals(s)["generator"] == "WordPress 6.9.4"

    def test_an_identifying_signal_latches(self):
        """One good observation is the whole answer; re-deriving per page is waste."""
        s = self._spider()
        s._record_platform_signals(self._page(
            body=b'<html><head><meta name="generator" content="WordPress 6.9.4"></head></html>'))
        assert s._platform_recorded is True
        s._record_platform_signals(self._page(
            body=b'<html><head><meta name="generator" content="Drupal 10"></head></html>',
            url="https://example.com/other"))
        assert self._signals(s)["generator"] == "WordPress 6.9.4"  # unchanged

    def test_non_200_and_non_text_are_ignored(self):
        s = self._spider()
        s._record_platform_signals(self._page(status=404))
        assert self._signals(s) is None

    def test_values_are_length_capped(self):
        s = self._spider()
        s._record_platform_signals(self._page(
            body=b'<html><head><meta name="generator" content="' + b"W" * 500
                 + b'"></head></html>'))
        assert len(self._signals(s)["generator"]) == WebsiteSpider._PLATFORM_VALUE_MAXLEN

    def test_a_site_that_identifies_itself_with_nothing_records_nothing(self):
        """Must stay absent, which the corpus reads as "no signal" — never as "not
        WordPress"."""
        s = self._spider()
        s._record_platform_signals(self._page())
        assert self._signals(s) is None

    def test_recording_never_raises(self):
        s = self._spider()
        s.crawler = types.SimpleNamespace(stats=None)
        s._record_platform_signals(self._page())  # no stats -> no-op, no exception


class TestSitemapProbe:
    """Issue #77: sitemap discovery was single-source. A site whose robots.txt omits the
    `Sitemap:` line got NO sitemap seeding and silently degraded to link-following only.
    gastro.org serves a complete 12-file Yoast index at /sitemap_index.xml (2,347 URLs) and
    names none of it in robots.txt."""

    SITEMAP = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               b'<url><loc>https://example.com/a</loc></url>'
               b'<url><loc>https://example.com/b</loc></url></urlset>')

    def _spider(self, robots=None):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        if robots is not None:
            from protego import Protego
            s._robots = Protego.parse(robots)
        return s

    def _robots(self, s, body):
        return list(s.parse_robots(TextResponse(
            url="https://example.com/robots.txt", body=body,
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"), status=200)))

    def _probe_resp(self, body=None, status=200, ctype="application/xml"):
        return TextResponse(
            url="https://example.com/sitemap_index.xml",
            body=self.SITEMAP if body is None else body,
            headers={"Content-Type": ctype},
            request=Request("https://example.com/sitemap_index.xml"), status=status)

    def test_probes_fire_when_robots_names_no_sitemap(self):
        s = self._spider()
        urls = [r.url for r in self._robots(s, b"User-agent: *\nAllow: /\n")
                if isinstance(r, Request)]
        for path in WebsiteSpider.SITEMAP_PROBE_PATHS:
            assert f"https://example.com{path}" in urls
        assert s.crawler.stats.values.get("seeding/sitemap_probes_sent") == 4

    def test_probes_do_not_fire_when_robots_names_one(self):
        """A site that points us at its sitemap gets no speculative traffic at all."""
        s = self._spider()
        urls = [r.url for r in self._robots(
            s, b"User-agent: *\nSitemap: https://example.com/custom-sitemap.xml\n")
            if isinstance(r, Request)]
        assert "https://example.com/custom-sitemap.xml" in urls
        assert not any("sitemap_index.xml" in u for u in urls)
        assert s.crawler.stats.values.get("seeding/sitemap_probes_sent") is None

    def test_a_404_probe_emits_no_row(self):
        """THE RISK THIS CHANGE COULD HAVE SHIPPED. `parse_sitemap` emits a row for every
        response, and most sites 404 three of the four guesses. Emitting those would put
        phantom broken links in the crawl -- and the report presents 404s as "broken links
        on the site" with a referrer. We would invent defects in a client's site and report
        them back."""
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp(body=b"<html>Not found</html>",
                                                          status=404, ctype="text/html")))
        assert out == []
        assert s.crawler.stats.values.get("seeding/sitemap_probes_missed") == 1

    def test_a_soft_404_html_page_is_not_treated_as_a_sitemap(self):
        """A catch-all route returning 200 with an HTML body is common; taking it for a
        sitemap would be worse than missing one."""
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp(
            body=b"<html><body>Page not found</body></html>", ctype="text/html")))
        assert out == []
        assert s.crawler.stats.values.get("seeding/sitemap_probes_not_a_sitemap") == 1

    def test_a_real_sitemap_is_followed(self):
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp()))
        urls = [r.url for r in out if isinstance(r, Request)]
        assert "https://example.com/a" in urls and "https://example.com/b" in urls
        assert s.crawler.stats.values.get("seeding/sitemap_probes_found") == 1

    def test_a_sitemap_index_is_followed(self):
        s = self._spider()
        body = (b'<?xml version="1.0"?><sitemapindex '
                b'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<sitemap><loc>https://example.com/post-sitemap.xml</loc></sitemap>'
                b'</sitemapindex>')
        out = list(s.parse_sitemap_probe(self._probe_resp(body=body)))
        # The nested sitemap is followed. (parse_sitemap's generic <loc> scan also schedules
        # it, which is pre-existing behavior and out of scope here.)
        assert "https://example.com/post-sitemap.xml" in [
            r.url for r in out if isinstance(r, Request)]

    def test_a_robots_disallowed_probe_path_is_not_fetched(self):
        """A probe is still a fetch. Guessing a URL is not a reason to stop obeying."""
        s = self._spider(robots="User-agent: *\nDisallow: /sitemap_index.xml\n")
        urls = [r.url for r in s._sitemap_probe_requests()]
        assert "https://example.com/sitemap_index.xml" not in urls
        assert "https://example.com/sitemap.xml" in urls
        assert s.crawler.stats.values.get("seeding/sitemap_probes_disallowed") == 1

    def test_a_failed_probe_is_counted_and_dropped(self):
        s = self._spider()
        s.sitemap_probe_failed(types.SimpleNamespace(value=OSError("refused")))
        assert s.crawler.stats.values.get("seeding/sitemap_probes_missed") == 1

    def test_probes_carry_an_errback(self):
        """Without one a refused probe would be an unhandled failure in the crawl log."""
        s = self._spider()
        assert all(r.errback is not None for r in s._sitemap_probe_requests())


class TestSitemapProbeHardening:
    """Findings from the #77 review. The first cut closed the phantom-404 risk at the probe
    RESPONSE and left it open one hop downstream, where it is unbounded."""

    SITEMAP = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               b'<url><loc>https://example.com/gone</loc></url></urlset>')

    def _spider(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _probe_resp(self, body=None, status=200, headers=None, meta=None,
                    url="https://example.com/sitemap.xml"):
        req = Request(url)
        req.meta.update(meta or {"guessed_source": True, "probe_hops": 0})
        h = {"Content-Type": "application/xml"}
        h.update(headers or {})
        return TextResponse(url=url, body=self.SITEMAP if body is None else body,
                            headers=h, request=req, status=status)

    def test_a_dead_url_from_a_guessed_sitemap_is_not_a_site_defect(self):
        """THE REVERSAL THIS COULD HAVE SHIPPED. A stale /sitemap.xml left over from a
        previous platform lists 400 pre-migration URLs. As normal rows those 400 404s make
        blocked_fraction 0.93 and flip the report to `wholesale_blocked` — telling the
        client "we couldn't read this site" about a site we read perfectly."""
        s = self._spider()
        req = Request("https://example.com/gone")
        req.meta["guessed_source"] = True
        dead = TextResponse(url="https://example.com/gone", body=b"<html>404</html>",
                            headers={"Content-Type": "text/html"}, request=req, status=404)
        out = list(s.parse(dead))
        assert len(out) == 1
        row = out[0]
        assert row["skip_reason"] == "guessed_sitemap_dead"
        assert row["status"] == 0, "a skip row is never a fetched page"
        assert s.crawler.stats.values.get("guessed_sitemap_dead_urls") == 1

    def test_a_live_url_from_a_guessed_sitemap_is_a_normal_page(self):
        """Only the DEAD ones are diverted — a guessed sitemap that is current is exactly
        the coverage this feature exists to recover."""
        s = self._spider()
        req = Request("https://example.com/real")
        req.meta["guessed_source"] = True
        live = TextResponse(url="https://example.com/real",
                            body=b"<html><body><h1>Real</h1></body></html>",
                            headers={"Content-Type": "text/html"}, request=req, status=200)
        rows = [o for o in s.parse(live) if isinstance(o, dict)]
        assert rows and rows[0]["skip_reason"] == ""
        assert rows[0]["status"] == 200

    def test_a_dead_url_from_a_LINKED_sitemap_is_still_a_normal_404(self):
        """A sitemap the site ADVERTISED is a positive assertion of currency, so a dead URL
        in it really is a broken link worth reporting. Only guesses are discounted."""
        s = self._spider()
        dead = TextResponse(url="https://example.com/gone", body=b"<html>404</html>",
                            headers={"Content-Type": "text/html"},
                            request=Request("https://example.com/gone"), status=404)
        rows = [o for o in s.parse(dead) if isinstance(o, dict)]
        assert rows and rows[0]["skip_reason"] == ""

    def test_provenance_reaches_the_scheduled_urls(self):
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp()))
        reqs = [r for r in out if isinstance(r, Request)]
        assert reqs and all(r.meta.get("guessed_source") for r in reqs)

    def test_a_linked_sitemap_marks_nothing_as_guessed(self):
        s = self._spider()
        resp = TextResponse(url="https://example.com/sitemap.xml", body=self.SITEMAP,
                            headers={"Content-Type": "application/xml"},
                            request=Request("https://example.com/sitemap.xml"), status=200)
        reqs = [r for r in s.parse_sitemap(resp) if isinstance(r, Request)]
        assert reqs and not any(r.meta.get("guessed_source") for r in reqs)

    def test_a_probe_redirect_is_followed_once(self):
        """A 301 is the strongest evidence a sitemap exists; dropping it lost the discovery
        for any site redirecting to a path we don't guess."""
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp(
            body=b"", status=301, headers={"Location": b"https://example.com/sitemaps/s.xml"})))
        assert [r.url for r in out] == ["https://example.com/sitemaps/s.xml"]
        assert out[0].meta["probe_hops"] == 1

    def test_a_probe_redirect_chain_is_bounded(self):
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp(
            body=b"", status=301, headers={"Location": b"https://example.com/again.xml"},
            meta={"guessed_source": True, "probe_hops": 1})))
        assert out == []

    def test_an_offdomain_probe_redirect_is_not_followed(self):
        s = self._spider()
        out = list(s.parse_sitemap_probe(self._probe_resp(
            body=b"", status=301, headers={"Location": b"https://evil.example/s.xml"})))
        assert out == []

    def test_probes_suppress_the_referer(self):
        """Otherwise the row asserts robots.txt linked to the sitemap — false in the only
        situation this code can run."""
        s = self._spider()
        assert all(r.headers.get("Referer") is None for r in s._sitemap_probe_requests())

    def test_root_element_is_anchored_not_merely_present(self):
        assert not WebsiteSpider._looks_like_sitemap(
            "<html><body>we support &lt;urlset&gt; sitemaps</body></html>")
        assert not WebsiteSpider._looks_like_sitemap("<html><urlset-ish></html>")

    def test_a_prologue_before_the_root_is_tolerated(self):
        """gastro.org's real sitemap opens with an <?xml-stylesheet?> PI."""
        assert WebsiteSpider._looks_like_sitemap(
            '<?xml version="1.0"?><?xml-stylesheet type="text/xsl" href="/main.xsl"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
        assert WebsiteSpider._looks_like_sitemap("﻿<!-- built by a plugin -->\n<urlset>")

    def test_a_namespace_prefixed_root_is_accepted(self):
        """The xpath this feeds uses local-name() and would have parsed it."""
        assert WebsiteSpider._looks_like_sitemap('<sm:urlset xmlns:sm="http://x">')


class TestTransportFailures:
    """Issue #73: a URL whose request never produced a response vanished from the crawl.

    Every exception here is the REAL class raised by the installed stack, imported rather
    than fabricated. A first cut of this suite built fixtures with
    `type("DNSLookupError", (Exception,), {})` -- it constructed the name it then asserted
    the table matched, so eight tests passed against a table that was entirely dead in
    production. Scrapy 2.18 wraps every Twisted download exception into its own class, and
    the --impersonate path raises curl_cffi's instead."""

    def _spider(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _failure(self, exc, url="https://example.com/gone"):
        return types.SimpleNamespace(value=exc, request=Request(url))

    def _kind(self, spider, exc, url="https://example.com/gone"):
        rows = list(spider.page_failed(self._failure(exc, url)))
        return rows[0]["skip_reason"] if rows else None

    def test_scrapys_own_wrapped_exceptions_are_classified(self):
        """These are what Scrapy 2.18 ACTUALLY raises -- it converts Twisted's exceptions in
        `scrapy/utils/_download_handlers.py` before any errback sees them."""
        from scrapy.exceptions import (
            CannotResolveHostError, DownloadConnectionRefusedError, DownloadFailedError,
            DownloadTimeoutError,
        )
        cases = [
            (CannotResolveHostError("no such host"), "unreachable_dns"),
            (DownloadTimeoutError("timed out"), "unreachable_timeout"),
            (DownloadConnectionRefusedError("refused"), "unreachable_connection"),
            (DownloadFailedError("reset"), "unreachable_connection"),
        ]
        for exc, expected in cases:
            assert self._kind(self._spider(), exc) == expected, type(exc).__name__

    def test_curl_cffi_exceptions_are_classified(self):
        """The --impersonate path is a second stack whose exceptions never pass through
        Scrapy's wrapper -- and it is the path used on the Cloudflare/Kinsta-fronted clients
        where reachability matters most."""
        from curl_cffi.requests import exceptions as ce
        cases = [
            (ce.DNSError("dns"), "unreachable_dns"),
            (ce.ConnectTimeout("slow"), "unreachable_timeout"),
            (ce.ReadTimeout("slow"), "unreachable_timeout"),
            (ce.ConnectionError("refused"), "unreachable_connection"),
            (ce.ProxyError("proxy"), "unreachable_connection"),
            (ce.CertificateVerifyError("bad cert"), "unreachable_tls"),
        ]
        for exc, expected in cases:
            assert self._kind(self._spider(), exc) == expected, type(exc).__name__

    def test_an_unknown_failure_degrades_to_other(self):
        """Honest: we saw a failure and cannot name it. Never a crash."""
        s = self._spider()
        assert self._kind(s, RuntimeError("something new")) == "unreachable_other"

    def test_a_dns_failure_emits_a_complete_skip_row(self):
        from scrapy.exceptions import CannotResolveHostError
        s = self._spider()
        rows = list(s.page_failed(self._failure(CannotResolveHostError("x"))))
        assert len(rows) == 1
        assert rows[0]["skip_reason"] == "unreachable_dns"
        assert rows[0]["url"] == "https://example.com/gone"
        assert rows[0]["status"] == 0, "never fetched, so never a status"
        assert s.crawler.stats.values.get("transport_failures/dns") == 1

    def test_our_own_ssrf_refusal_is_not_reported_as_a_site_failure(self):
        """IgnoreRequest is OUR middleware declining the request. Reporting it as "we
        couldn't reach this page" blames the site for our decision."""
        from scrapy.exceptions import IgnoreRequest
        s = self._spider()
        assert list(s.page_failed(self._failure(IgnoreRequest("ssrf")))) == []
        assert s.crawler.stats.values.get("transport_failures") is None
        assert s.crawler.stats.values.get("transport_failures_not_reported") == 1

    def test_a_size_capped_download_is_not_reported_as_unreachable(self):
        """THE REVERSAL THIS COULD HAVE SHIPPED, and the guard that missed it. Scrapy raises
        DownloadCancelledError when a body crosses DOWNLOAD_MAXSIZE (64MB) -- an
        extensionless download endpoint serving a 90MB PDF is fetched as a page and aborted
        mid-stream. The server answered perfectly; WE hung up. The first cut guarded the
        Twisted name `CancelledError`, which Scrapy 2.18 never surfaces, so the report would
        have told the client that URL "never responded -- the host may be down or gone"."""
        from scrapy.exceptions import DownloadCancelledError
        s = self._spider()
        assert list(s.page_failed(self._failure(DownloadCancelledError("too big")))) == []
        assert s.crawler.stats.values.get("transport_failures") is None

    def test_the_failed_url_keeps_its_identity(self):
        """SCHEDULE-normalized, not emit-normalized. run_spider always sets
        reach_pagination=1, so emit-mode strips `?page=N` -- emitting `/blog/?page=14` as
        `/blog/` would list a URL that crawled fine under "never responded", and the corpus
        upsert keyed on (crawl_id, url) would collapse 18 failed pagination pages into one
        row with the wrong name."""
        from scrapy.exceptions import DownloadTimeoutError
        s = WebsiteSpider(domain="example.com", reach_pagination="1")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        rows = list(s.page_failed(
            self._failure(DownloadTimeoutError("x"), "https://example.com/blog/?page=14")))
        assert rows[0]["url"] == "https://example.com/blog/?page=14"

    def test_the_referrer_is_carried_through(self):
        """Which page linked to the dead host is the actionable half of the signal."""
        from scrapy.exceptions import CannotResolveHostError
        s = self._spider()
        key = s.facet_dedup_key(s.normalize_url("https://example.com/gone",
                                                exclude_params=s.exclude_params_schedule))
        s.first_referrer[key] = "https://example.com/sponsors"
        rows = list(s.page_failed(self._failure(CannotResolveHostError("x"))))
        assert rows[0]["referrer"] == "https://example.com/sponsors"

    def test_scheduled_requests_carry_the_errback(self):
        s = self._spider()
        reqs = list(s._schedule("https://example.com/about"))
        assert reqs and all(r.errback == s.page_failed for r in reqs)

    def test_asset_head_requests_carry_the_errback(self):
        s = self._spider()
        reqs = list(s._schedule("https://example.com/brochure.pdf"))
        assert reqs and all(r.errback == s.page_failed for r in reqs)

    def test_every_scheduled_request_in_the_spider_has_an_errback(self):
        """A request without one loses its failure silently -- the bug this issue is about."""
        import re as _re, inspect as _inspect
        src = _inspect.getsource(WebsiteSpider)
        calls = _re.findall(r"scrapy\.Request\((.*?)\)\n", src, _re.DOTALL)
        missing = [c for c in calls if "errback" not in c]
        assert not missing, f"{len(missing)} scrapy.Request(...) without an errback"

    def test_a_failure_with_no_request_does_not_raise(self):
        from scrapy.exceptions import CannotResolveHostError
        s = self._spider()
        exc = CannotResolveHostError("x")
        assert list(s.page_failed(types.SimpleNamespace(value=exc))) == []
        assert s.crawler.stats.values.get("transport_failures") == 1


class TestSchemeConfusion:
    """Issue #89: `is_internal` compared the HOST and never the scheme, so a URL naming any
    protocol was accepted as long as its hostname matched the crawled domain.

    FIVE intake paths take a value the REMOTE SERVER controls and had `is_internal` as their
    only validation: a `Location:` on a robots.txt redirect, a `Sitemap:` line, a sitemap
    `<loc>`, and the `Location:` on a page redirect and on an asset redirect. The first
    version of this docstring said three, and also said the `<a href>` path was already safe
    via `_NONNAV_SCHEMES` -- it was not, because `s3:` was missing from that tuple. Both
    errors argue the same thing: the gate belongs in `is_internal`, where failing closed does
    not depend on enumerating the call sites correctly.
    """

    def _spider(self, domain="example.com", **kw):
        s = WebsiteSpider(domain=domain, **kw)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_the_library_behaviour_that_makes_this_exploitable(self):
        """Pinned against the REAL w3lib, because the whole finding rests on it: the host is
        DISCARDED, so `file://<crawled-domain>/x` reads `/x` off the crawl host. Asserting
        our own rejection without this would leave the reason unrecorded."""
        from w3lib.url import file_uri_to_path
        assert file_uri_to_path("file://example.com/etc/passwd") == "/etc/passwd"
        from scrapy.settings import default_settings as d
        # ... and Scrapy has a handler ready to act on it.
        assert "file" in d.DOWNLOAD_HANDLERS_BASE
        assert {"ftp", "s3", "data"} <= set(d.DOWNLOAD_HANDLERS_BASE)

    def test_non_http_schemes_are_refused_despite_a_matching_host(self):
        s = self._spider()
        for url in (
            "file://example.com/etc/passwd",
            "file://www.example.com/etc/passwd",
            "ftp://example.com/x",
            "s3://example.com/x",
            "data:text/plain,hi",
            "javascript:alert(1)",
        ):
            assert s.is_internal(url) is False, url

    def test_http_and_https_are_unaffected(self):
        s = self._spider()
        assert s.is_internal("https://example.com/page") is True
        assert s.is_internal("http://example.com/page") is True
        assert s.is_internal("https://www.example.com/page") is True
        assert s.is_internal("https://elsewhere.test/page") is False

    def test_subdomain_mode_is_unaffected(self):
        s = self._spider(include_subdomains=True)
        assert s.is_internal("https://blog.example.com/x") is True
        assert s.is_internal("file://blog.example.com/etc/passwd") is False

    def test_uppercase_scheme_still_matches(self):
        """A site controls the exact bytes; `FILE://` must not slip past a lowercase set,
        and `HTTPS://` must not be refused."""
        s = self._spider()
        assert s.is_internal("HTTPS://example.com/x") is True
        assert s.is_internal("FILE://example.com/etc/passwd") is False

    def test_protocol_relative_is_a_clean_skip_not_an_exception(self):
        """`Sitemap:` lines and `<loc>` values are NOT urljoined, so `//host/x` used to pass
        the host check and reach `scrapy.Request`, which RAISES. Verified against the real
        Request, so this pins the bug the scheme term also fixes."""
        s = self._spider()
        assert s.is_internal("//example.com/sitemap.xml") is False
        with pytest.raises(ValueError):
            Request("//example.com/sitemap.xml")

    def test_a_hostile_robots_redirect_is_refused_AND_still_seeds(self):
        """The load-bearing one. Refusing the hop must not cost the crawl its seed --
        turning a hostile redirect into a zero-page crawl would be the #76 failure, which is
        strictly worse than the redirect."""
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/robots.txt", body=b"", status=301,
            headers={"Location": b"file://example.com/etc/passwd"},
            request=Request("https://example.com/robots.txt"),
        )
        assert s._robots_redirect_request(resp) is None
        out = [r for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert "https://example.com/" in [r.url for r in out]
        assert not any(r.url.startswith("file:") for r in out)
        assert s.crawler.stats.values.get("seeding/start_urls_emitted") == 1

    def test_a_hostile_sitemap_line_in_robots_is_refused(self):
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/robots.txt",
            body=b"User-agent: *\nAllow: /\nSitemap: file://example.com/etc/passwd\n",
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert not any(u.startswith("file:") for u in urls)
        # Refused, so robots named no USABLE sitemap -- the conventional locations are
        # probed, exactly as for a robots.txt that named none at all.
        assert any("sitemap" in u for u in urls)
        assert "https://example.com/" in urls

    def test_a_hostile_loc_in_a_sitemap_is_refused(self):
        s = self._spider()
        body = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>file://example.com/etc/passwd</loc></url>"
                b"<url><loc>https://example.com/real-page/</loc></url></urlset>")
        resp = TextResponse(
            url="https://example.com/sitemap.xml", body=body,
            headers={"Content-Type": "application/xml"},
            request=Request("https://example.com/sitemap.xml"),
        )
        rows = list(s.parse_sitemap(resp))
        urls = [r.url for r in rows if isinstance(r, Request)]
        assert not any(u.startswith("file:") for u in urls)
        assert "https://example.com/real-page/" in urls

    def test_a_hostile_nested_sitemap_index_is_refused(self):
        s = self._spider()
        body = (b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<sitemap><loc>ftp://example.com/evil.xml</loc></sitemap>"
                b"<sitemap><loc>https://example.com/sitemap-2.xml</loc></sitemap></sitemapindex>")
        resp = TextResponse(
            url="https://example.com/sitemap.xml", body=body,
            headers={"Content-Type": "application/xml"},
            request=Request("https://example.com/sitemap.xml"),
        )
        urls = [r.url for r in s.parse_sitemap(resp) if isinstance(r, Request)]
        assert not any(u.startswith("ftp:") for u in urls)
        assert "https://example.com/sitemap-2.xml" in urls

    def test_schedule_refuses_a_non_http_url(self):
        """`_schedule` is the funnel every discovered URL passes through."""
        s = self._spider()
        assert list(s._schedule("file://example.com/etc/passwd")) == []
        assert len(list(s._schedule("https://example.com/ok/"))) == 1


class TestSchemeGateHardening:
    """Findings from the #89 review: the ways a gate documented as failing CLOSED did not."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_a_malformed_url_returns_false_instead_of_raising(self):
        """`urlparse` raises on a bad netloc, and this gate runs mid-iteration over a
        SITE-SUPPLIED list -- so one malformed value aborted the loop and cost every later
        entry. Verified against the real urlparse, which is what raises."""
        from urllib.parse import urlparse as _up
        with pytest.raises(ValueError):
            _up("http://[::1/x")          # the real raise this guards
        s = self._spider()
        assert s.is_internal("http://[::1/x") is False

    def test_one_malformed_loc_does_not_cost_the_rest_of_the_sitemap(self):
        """The consequence that makes it more than cosmetic: a site hands us a broken URL
        and gets the remainder of its sitemap silently dropped."""
        s = self._spider()
        body = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>http://[::1/broken</loc></url>"
                b"<url><loc>https://example.com/real-page/</loc></url></urlset>")
        resp = TextResponse(url="https://example.com/sitemap.xml", body=body,
                            headers={"Content-Type": "application/xml"},
                            request=Request("https://example.com/sitemap.xml"))
        urls = [r.url for r in s.parse_sitemap(resp) if isinstance(r, Request)]
        assert "https://example.com/real-page/" in urls

    def test_a_relative_sitemap_line_is_recovered_not_dropped(self):
        """robots.txt is supposed to name an ABSOLUTE Sitemap: URL and plenty of sites do
        not. Refusing scheme-less values without urljoining first turned that into silent
        coverage loss -- the half my first docstring missed."""
        s = self._spider()
        # Deliberately NOT a path in SITEMAP_PROBE_PATHS. The first version of this test used
        # `/sitemap.xml`, which the #77 probe fallback also emits -- so it passed with the fix
        # reverted and proved nothing.
        assert "/news-sitemap.xml" not in WebsiteSpider.SITEMAP_PROBE_PATHS
        resp = TextResponse(
            url="https://example.com/robots.txt",
            body=b"User-agent: *\nAllow: /\nSitemap: /news-sitemap.xml\n",
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert "https://example.com/news-sitemap.xml" in urls

    def test_a_protocol_relative_sitemap_line_is_recovered(self):
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/robots.txt",
            body=b"User-agent: *\nAllow: /\nSitemap: //example.com/sm.xml\n",
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert "https://example.com/sm.xml" in urls

    def test_urljoin_does_not_launder_a_hostile_scheme(self):
        """The reason urljoin is safe here: an absolute scheme passes through it UNCHANGED,
        so the gate still sees `file://` and still refuses it. And a protocol-relative
        off-domain host resolves to https and is then refused on the host compare."""
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/robots.txt",
            body=(b"User-agent: *\nAllow: /\nSitemap: file://example.com/etc/passwd\n"
                  b"Sitemap: //evil.test/sm.xml\n"),
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        assert not any(u.startswith("file:") for u in urls)
        assert not any("evil.test" in u for u in urls)

    def test_a_refused_sitemap_line_does_not_suppress_the_probe_fallback(self):
        """`listed_a_sitemap` gates the #77 conventional-location probes. Setting it before
        the Request existed meant a Sitemap: line we could not queue still cost us the
        fallback that exists precisely to cover a site with no usable sitemap."""
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/robots.txt",
            body=b"User-agent: *\nAllow: /\nSitemap: file://example.com/etc/passwd\n",
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )
        urls = [r.url for r in s.parse_robots(resp) if isinstance(r, Request)]
        probes = [u for u in urls if any(u.endswith(p) for p in WebsiteSpider.SITEMAP_PROBE_PATHS)]
        assert probes, "the probe fallback was suppressed by a sitemap we never queued"

    def test_the_guard_and_the_spider_agree_on_what_is_fetchable(self):
        """The constant is duplicated on purpose (decoupling a security guard from the module
        it guards). Duplication is only safe while they cannot disagree in the OPEN direction."""
        from ssrf_guard import SsrfGuardMiddleware
        assert WebsiteSpider._FETCHABLE_SCHEMES <= SsrfGuardMiddleware._FETCHABLE_SCHEMES


class TestFetchGateVsSameSite:
    """#89 review: `is_internal` now answers a different question than it used to.

    It is a FETCH gate (may we hand this to a download handler?). `content_extractor` was
    using the same callable to CLASSIFY links (does this point at the client's own site?),
    where the scheme is irrelevant -- so hardening one silently changed the other."""

    def _counts(self, html, page_url="https://example.com/page", predicate=None):
        from lxml import html as lxml_html
        from content_extractor import count_structure
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return count_structure(
            lxml_html.fromstring(html), page_url,
            is_internal=predicate or s.is_same_site,
            asset_extensions=WebsiteSpider.ASSET_EXTENSIONS,
        )

    HTML = ('<html><body><main><p>x</p>'
            '<a href="ftp://example.com/downloads/report.pdf">files</a>'
            '<a href="https://vendor.test/app">vendor</a>'
            '</main></body></html>')

    def test_an_ftp_link_to_our_own_domain_is_not_reported_as_a_third_party(self):
        """`<a href="ftp://<our-domain>/downloads">` is not rare on older association sites.
        Under the fetch gate it stopped counting as internal, fell through to the external
        branch, and listed the client's OWN domain back to them as an external integration."""
        c = self._counts(self.HTML)
        assert "example.com" not in (c.get("external_link_hosts") or [])
        assert "vendor.test" in (c.get("external_link_hosts") or [])

    def test_the_fetch_gate_would_have_gotten_this_wrong(self):
        """Pins WHY the predicate had to be split, rather than just asserting the fix."""
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        c = self._counts(self.HTML, predicate=s.is_internal)
        assert "example.com" in (c.get("external_link_hosts") or []), (
            "if this stops being true the split is no longer load-bearing"
        )

    def test_the_two_predicates_differ_only_on_scheme(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        # Same answer for anything fetchable ...
        for u in ("https://example.com/x", "http://www.example.com/x",
                  "https://elsewhere.test/x"):
            assert s.is_internal(u) == s.is_same_site(u), u
        # ... and they diverge exactly where they should.
        assert s.is_internal("ftp://example.com/x") is False
        assert s.is_same_site("ftp://example.com/x") is True
        # Neither may raise on a malformed URL.
        assert s.is_same_site("http://[::1/x") is False

    def test_the_fetch_gate_is_what_still_guards_scheduling(self):
        """The split must not loosen the thing #89 fixed."""
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        assert list(s._schedule("ftp://example.com/x")) == []
        assert list(s._schedule("file://example.com/etc/passwd")) == []


class TestRemoteRedirectsCannotSmuggleAScheme:
    """The two intake paths the first version of the #89 comment MISSED: a page redirect and
    an asset redirect both feed a remote `Location:` into `_schedule`."""

    def _spider(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_a_page_redirect_to_file_is_refused(self):
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/page", body=b"", status=301,
            headers={"Location": b"file://example.com/etc/passwd"},
            request=Request("https://example.com/page"),
        )
        urls = [r.url for r in s.parse(resp) if isinstance(r, Request)]
        assert not any(u.startswith("file:") for u in urls)

    def test_an_href_with_a_handler_scheme_is_not_navigational(self):
        """`s3:` was absent from _NONNAV_SCHEMES, which is what made the original comment's
        "<a href> was already safe" claim false."""
        s = self._spider()
        assert s.is_navigational_href("s3://example.com/x") is False
        assert s.is_internal("s3://example.com/x") is False


class TestRobotsFetchBudget:
    """Issue #82: since #76 robots.txt is the crawl's ONLY seed, so nothing else is in
    flight while it retries. A black-holed host now stalls the whole crawl.

    Every bound here is asserted against the REAL middleware/parser that consumes it, not
    against the meta dict we wrote -- the two download paths read different keys, and
    scrapy-impersonate reads neither `download_timeout` nor DOWNLOAD_TIMEOUT. A test that
    only checked our own meta would pass while the impersonate path stayed unbounded."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _seed(self, spider):
        return next(iter(spider._seed_requests()))

    def test_the_stall_being_fixed_is_real_arithmetic(self):
        """Pin the numbers the fix is sized against, from Scrapy's own defaults."""
        from scrapy.settings import default_settings as d
        from scrapy.exceptions import DownloadTimeoutError
        # A timeout IS retried, so the stall is (RETRY_TIMES + 1) whole timeouts.
        assert "scrapy.exceptions.DownloadTimeoutError" in d.RETRY_EXCEPTIONS
        assert DownloadTimeoutError  # imported: the name in RETRY_EXCEPTIONS resolves
        assert (d.RETRY_TIMES + 1) * d.DOWNLOAD_TIMEOUT == 540
        # ... against a 7200s session budget. The bound has to be a real fraction of that.
        assert (d.RETRY_TIMES + 1) * WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT == 180

    def test_default_path_honours_our_bound_over_the_setting(self):
        """DownloadTimeoutMiddleware uses `setdefault`, so ours must survive it. If that
        ever became a plain assignment the crawl would silently go back to 180s."""
        from scrapy.downloadermiddlewares.downloadtimeout import DownloadTimeoutMiddleware
        req = self._seed(self._spider())
        DownloadTimeoutMiddleware(180).process_request(req)
        assert req.meta["download_timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT

    def test_impersonate_path_is_bounded_too(self):
        """scrapy-impersonate hands curl_cffi whatever RequestParser produces and adds no
        timeout of its own, so an unbounded robots fetch there inherits curl_cffi's 30s
        session default -- a bound we never chose and cannot see."""
        from scrapy_impersonate.parser import RequestParser
        req = self._seed(self._spider())
        args = RequestParser(req).as_dict()
        assert args["timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT

    def test_impersonate_ignores_the_key_the_default_path_uses(self):
        """The reason `impersonate_args` is needed at all. Guards the day scrapy-impersonate
        starts honouring `download_timeout`: this test failing means the second key is now
        redundant, not that something broke."""
        from scrapy_impersonate.parser import RequestParser
        req = Request("https://example.com/", meta={"download_timeout": 5})
        assert "timeout" not in RequestParser(req).as_dict()

    def test_every_robots_hop_carries_the_bound(self):
        """MAX_ROBOTS_REDIRECTS means up to four fetches, each of them the crawl's only
        request. Bounding only the first leaves three unbounded stalls behind it."""
        from scrapy_impersonate.parser import RequestParser
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/robots.txt", body=b"", status=301,
            headers={"Location": b"https://example.com/robots.txt?x=1"},
            request=Request("https://example.com/robots.txt"),
        )
        hop = s._robots_redirect_request(resp)
        assert hop.meta["download_timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
        assert RequestParser(hop).as_dict()["timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
        # ... without losing the hop counter that bounds the chain in the first place.
        assert hop.meta["robots_hops"] == 1

    def test_pages_keep_the_full_timeout(self):
        """The bound is sized for 1KB of text and must not leak onto real pages, which are
        the thing DOWNLOAD_TIMEOUT is actually sized for."""
        from scrapy.downloadermiddlewares.downloadtimeout import DownloadTimeoutMiddleware
        s = self._spider()
        list(s.parse_robots(TextResponse(
            url="https://example.com/robots.txt", body=b"User-agent: *\nAllow: /\n",
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )))
        page = next(iter(s._schedule("https://example.com/a-page/")))
        DownloadTimeoutMiddleware(180).process_request(page)
        assert page.meta["download_timeout"] == 180

    def test_a_slow_but_real_robots_txt_is_not_treated_as_absent(self):
        """The asymmetry that sets the number. Timing out routes to `robots_failed`, which
        proceeds ALLOW-ALL -- so too short a bound silently converts "slow site" into "no
        robots.txt" and crawls a site that said Disallow, which is #76's harm by another
        door. 60s for 1KB is unreachable by any host that could serve a crawl at all."""
        assert WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT >= 60, (
            "below 60s a slow-but-real robots.txt times out into robots_failed, which "
            "proceeds ALLOW-ALL -- _robots_budget_meta explicitly calls 30s inside the "
            "range a real site can take"
        )
        s = self._spider("gastro.org")
        failure = types.SimpleNamespace(value=OSError("timed out"))
        list(s.robots_failed(failure))
        assert not s.is_robots_disallowed("https://gastro.org/anything/")


class TestStartUrlDepth:
    """Issue #81: since #76 the start URL is yielded from `parse_robots`, so DepthMiddleware
    reads it as a child of the robots.txt response and stamps depth=1.

    Asserted by running the REAL DepthMiddleware over real spider output. Reading
    `request.meta["depth"]` off our own request proves nothing -- the middleware is what
    assigns it, and it assigns unconditionally."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _mw(self):
        from scrapy.spidermiddlewares.depth import DepthMiddleware
        return DepthMiddleware(0, self._stats(), False, 1)

    @staticmethod
    def _stats():
        """DepthMiddleware calls `max_value`, which _FakeStats does not carry."""
        class _S(_FakeStats):
            def max_value(self, key, value):
                self.values[key] = max(self.values.get(key, value), value)
        return _S()

    def _robots_response(self, body=b"User-agent: *\nAllow: /\n"):
        resp = TextResponse(
            url="https://example.com/robots.txt", body=body,
            headers={"Content-Type": "text/plain"},
            request=Request("https://example.com/robots.txt"),
        )
        resp.meta["depth"] = 0  # what DepthMiddleware assigns to the seed
        return resp

    def _depths(self, body=b"User-agent: *\nAllow: /\n"):
        s = self._spider()
        resp = self._robots_response(body)
        mw = self._mw()
        out = {}
        for req in s.parse_robots(resp):
            if isinstance(req, Request):
                processed = mw.get_processed_request(req, resp)
                out[processed.url] = (processed.meta["depth"], processed.priority)
        return out

    def test_start_url_is_back_at_depth_zero(self):
        assert self._depths()["https://example.com/"][0] == 0

    def test_meta_depth_zero_would_not_have_worked(self):
        """The shape #81 suggested. DepthMiddleware ASSIGNS meta["depth"], so a pre-set
        value is overwritten -- the fix would have read as applied and changed nothing.
        This is the test that says why `depth_reset` is the key."""
        resp = self._robots_response()
        naive = Request("https://example.com/", meta={"depth": 0})
        assert self._mw().get_processed_request(naive, resp).meta["depth"] == 1

    def test_a_future_depth_limit_now_means_what_it_says(self):
        """The whole point: DEPTH_LIMIT=1 must reach the homepage's links, not stop at the
        homepage. Before the fix the homepage itself consumed the only level."""
        from scrapy.spidermiddlewares.depth import DepthMiddleware
        s = self._spider()
        robots = self._robots_response()
        mw = DepthMiddleware(1, self._stats(), False, 1)
        home = next(r for r in s.parse_robots(robots)
                    if isinstance(r, Request) and r.url == "https://example.com/")
        home = mw.get_processed_request(home, robots)
        assert home is not None and home.meta["depth"] == 0
        home_resp = TextResponse(url="https://example.com/", body=b"<html></html>",
                                 request=home, status=200)
        home_resp.meta.update(home.meta)
        link = mw.get_processed_request(Request("https://example.com/about/"), home_resp)
        assert link is not None, "DEPTH_LIMIT=1 must still reach the homepage's own links"
        assert link.meta["depth"] == 1

    def test_redirected_robots_does_not_push_the_start_url_deeper(self):
        """The two-off case: each redirect hop was another level of drift."""
        s = self._spider()
        mw = self._mw()
        first = self._robots_response()
        hop = next(r for r in s.parse_robots(TextResponse(
            url="https://example.com/robots.txt", body=b"", status=301,
            headers={"Location": b"https://example.com/robots.txt?x=1"},
            request=Request("https://example.com/robots.txt"),
        )) if isinstance(r, Request))
        hop = mw.get_processed_request(hop, first)
        # The hop is depth 0, not 1: it carries `depth_reset` too (#82/#81 review). Without
        # that, a DEPTH_LIMIT of 1 makes DepthMiddleware return None for the SECOND hop --
        # dropping the request seeding was deferred to, for a zero-page crawl that closes
        # `finished`. Reproduced on a real engine before this was added.
        assert hop.meta["depth"] == 0
        hop_resp = self._robots_response()
        hop_resp.meta["depth"] = hop.meta["depth"]
        start = next(r for r in s.parse_robots(hop_resp)
                     if isinstance(r, Request) and r.url == "https://example.com/")
        assert mw.get_processed_request(start, hop_resp).meta["depth"] == 0

    def test_homepage_links_outrank_sitemap_urls_again(self):
        """DEPTH_PRIORITY=1 makes depth the BFO tiebreak. With the start URL at depth 1 its
        links landed at depth 2, TYING with sitemap-listed URLs instead of outranking them;
        #54 is about ordering, so this is not cosmetic."""
        depths = self._depths(
            b"User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n")
        home_depth, home_prio = depths["https://example.com/"]
        sm_depth, sm_prio = depths["https://example.com/sitemap.xml"]
        assert (home_depth, sm_depth) == (0, 1)
        # A homepage link is depth 1; a sitemap-listed URL is depth 2. Lower depth wins.
        assert home_depth + 1 < sm_depth + 1
        assert home_prio > sm_prio

    def test_the_transport_failure_path_lands_at_depth_zero_by_a_DIFFERENT_mechanism(self):
        """The errback path gets the right depth, but NOT from `depth_reset`.

        The first version of this test asserted `start.meta["depth_reset"] is True` -- our
        own dict, which this class's own docstring says proves nothing, and on the one path
        where the key is consumed by nobody. Scrapy runs NO spider middleware on errback
        output (`Scraper._scrape` wraps only the Response branch in `spidermw`), so
        DepthMiddleware never sees these requests. Depth 0 comes from `_init_depth`'s base
        case instead: no `depth` in the response meta means the response IS depth 0.

        So both seeding paths reach depth 0 by two different mechanisms, and this asserts the
        outcome on the path the middleware never touches."""
        from scrapy.spidermiddlewares.depth import DepthMiddleware
        s = self._spider()
        out = list(s.robots_failed(types.SimpleNamespace(value=OSError("dns"))))
        start = next(r for r in out if r.url == "https://example.com/")
        # The engine hands the errback's requests straight to the scheduler; nothing
        # consumes depth_reset, so the response arrives with no `depth` at all.
        assert "depth" not in start.meta
        home = TextResponse(url="https://example.com/", body=b"<html></html>",
                            request=start, status=200)
        home.meta.update(start.meta)
        mw = DepthMiddleware(0, self._stats(), False, 1)
        list(mw.process_spider_output(home, iter([])))
        assert home.meta["depth"] == 0, "_init_depth's base case is what saves this path"
        link = mw.get_processed_request(Request("https://example.com/about/"), home)
        assert link.meta["depth"] == 1

    def test_sitemap_probes_get_the_same_depth_on_both_seeding_paths(self):
        """Same site, same probes, different depth depending on whether robots.txt answered
        -- because one path runs DepthMiddleware and the other does not. Pinned explicitly
        so a future DEPTH_LIMIT cannot bound sitemap discovery differently by transport luck."""
        s = self._spider()
        failed = [r for r in s.robots_failed(types.SimpleNamespace(value=OSError("dns")))
                  if r.url.endswith(".xml")]
        assert failed and all(r.meta["depth"] == 1 for r in failed)

        s2 = self._spider()
        resp = self._robots_response(b"User-agent: *\nAllow: /\n")
        mw = self._mw()
        probed = [r for r in s2.parse_robots(resp)
                  if isinstance(r, Request) and r.url.endswith(".xml")]
        assert probed
        for r in probed:
            assert mw.get_processed_request(r, resp).meta["depth"] == 1

    def test_the_installed_scrapy_actually_supports_depth_reset(self):
        """`depth_reset` landed in Scrapy 2.18 and is an unrecognised key before that --
        ignored in SILENCE, which is exactly the failure #81 is about. requirements.txt
        floors Scrapy at 2.18; this asserts the floor is doing its job here."""
        from website_spider import _DEPTH_RESET_SUPPORTED, _depth_reset_supported
        assert _DEPTH_RESET_SUPPORTED is True
        assert _depth_reset_supported() is True

    def test_the_detector_reports_unsupported_rather_than_raising(self, monkeypatch):
        """It runs at import time, so a Scrapy that moved DepthMiddleware must degrade to a
        warning, never take the module down.

        monkeypatch, not a manual save/restore: this mutates a process-global that every
        other test in the session shares, and a failure between the assignment and the
        `finally` would leave the module broken for them (and it is outright unsafe under
        pytest-xdist)."""
        import website_spider as ws
        import scrapy.spidermiddlewares.depth as depth_mod
        monkeypatch.setattr(depth_mod, "DepthMiddleware", None)
        assert ws._depth_reset_supported() is False

    def test_the_detector_rejects_a_scrapy_that_ACCEPTS_the_key_but_ignores_it(self,
                                                                               monkeypatch):
        """The branch that matters, and the one the first cut left untested.

        Mutation-testing the probe showed that neutering the correctness half of
        `return out is not None and out.meta.get("depth") == 0` -- leaving `out is not None`
        -- kept all 15 tests green. The suite could tell "raised" from "real Scrapy 2.18",
        but not "ran fine and got the depth WRONG", which is precisely the silent no-op #81
        is about: a guard against silent failure that could itself fail silently."""
        import website_spider as ws
        import scrapy.spidermiddlewares.depth as depth_mod

        class _IgnoresDepthReset:
            """A pre-2.18 DepthMiddleware: the key is unknown, so depth is still parent+1."""
            def __init__(self, *a, **k): pass

            def get_processed_request(self, request, response):
                request.meta["depth"] = response.meta["depth"] + 1
                return request

        monkeypatch.setattr(depth_mod, "DepthMiddleware", _IgnoresDepthReset)
        assert ws._depth_reset_supported() is False

    def test_the_unsupported_case_is_counted_not_just_logged(self):
        """The house convention for this failure class
        (docs/solutions/conventions/silent-orphaning-framework-extension-points.md) is a
        stat surfaced in the status file, not a log line on a hand-managed droplet."""
        import website_spider as ws
        s = self._spider()
        list(s._start_url_requests())
        assert s.crawler.stats.values.get("seeding/depth_reset_unsupported") == 0

        s2 = self._spider()
        ws_flag = ws._DEPTH_RESET_SUPPORTED
        try:
            ws._DEPTH_RESET_SUPPORTED = False
            out = list(s2._start_url_requests())
        finally:
            ws._DEPTH_RESET_SUPPORTED = ws_flag
        assert s2.crawler.stats.values.get("seeding/depth_reset_unsupported") == 1
        # ... and seeding still happens. A loud warning that also stops the crawl would be
        # a worse bug than the off-by-one it reports.
        assert [r.url for r in out] == ["https://example.com/"]


class TestSeedingSurvivesADepthBound:
    """#82/#81 review: the combination that turns the #81 fix into a zero-page crawl.

    `test_a_future_depth_limit_now_means_what_it_says` certifies DEPTH_LIMIT against the
    homepage, which has no redirect hop -- so it certified a property the robots.txt path
    did not have. Seeding is deferred to a callback now, so ANY seeding request dropped by a
    depth bound is a crawl with nothing to do."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    @staticmethod
    def _stats():
        class _S(_FakeStats):
            def max_value(self, key, value):
                self.values[key] = max(self.values.get(key, value), value)
        return _S()

    def _mw(self, maxdepth):
        from scrapy.spidermiddlewares.depth import DepthMiddleware
        return DepthMiddleware(maxdepth, self._stats(), False, 1)

    def _redirect(self, url, location):
        return TextResponse(url=url, body=b"", status=301,
                            headers={"Location": location.encode()},
                            request=Request(url))

    def test_a_two_hop_robots_redirect_still_seeds_under_a_depth_limit(self):
        """The reproduced failure: at DEPTH_LIMIT=1 the second hop was dropped by
        DepthMiddleware, so `parse_robots` never ran again and the start URL was never
        emitted -- 2 rows, both robots.txt, closing `finished`."""
        s = self._spider()
        mw = self._mw(1)
        parent = self._redirect("https://example.com/robots.txt",
                                "https://example.com/robots2.txt")
        parent.meta["depth"] = 0

        hop1 = next(r for r in s.parse_robots(parent) if isinstance(r, Request))
        hop1 = mw.get_processed_request(hop1, parent)
        assert hop1 is not None, "hop 1 dropped by DEPTH_LIMIT"
        r1 = self._redirect(hop1.url, "https://example.com/robots3.txt")
        r1.meta.update(hop1.meta)

        hop2 = next(r for r in s.parse_robots(r1) if isinstance(r, Request))
        hop2 = mw.get_processed_request(hop2, r1)
        assert hop2 is not None, "hop 2 dropped by DEPTH_LIMIT -- seeding never resumes"

        final = TextResponse(url=hop2.url, body=b"User-agent: *\nAllow: /\n",
                             headers={"Content-Type": "text/plain"},
                             request=Request(hop2.url))
        final.meta.update(hop2.meta)
        seeded = [r for r in s.parse_robots(final) if isinstance(r, Request)]
        start = [r for r in seeded if r.url == "https://example.com/"]
        assert start, "the start URL was never emitted -- zero-page crawl"
        assert mw.get_processed_request(start[0], final) is not None
        assert s.crawler.stats.values.get("seeding/start_urls_emitted") == 1

    def test_sitemap_probes_survive_a_depth_limit_of_one(self):
        """Probes sit at depth 1, so DEPTH_LIMIT=1 must still admit them -- otherwise a
        depth bound silently disables sitemap discovery rather than bounding the crawl."""
        s = self._spider()
        mw = self._mw(1)
        resp = TextResponse(url="https://example.com/robots.txt",
                            body=b"User-agent: *\nAllow: /\n",
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"))
        resp.meta["depth"] = 0
        probes = [r for r in s.parse_robots(resp)
                  if isinstance(r, Request) and r.url.endswith(".xml")]
        assert probes
        assert all(mw.get_processed_request(r, resp) is not None for r in probes)


class TestRobotsBudgetCoverage:
    """#82 review: the budget has to reach every request on the seeding path, survive the
    machinery between us and the downloader, and stay off everything else."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def _robots(self, body=b"User-agent: *\nAllow: /\n"):
        return TextResponse(url="https://example.com/robots.txt", body=body,
                            headers={"Content-Type": "text/plain"},
                            request=Request("https://example.com/robots.txt"))

    def test_sitemap_probes_carry_the_budget(self):
        """Same seeding path, same stall class. Under `--profile presale`
        (CONCURRENT_REQUESTS 1) four unbounded probes are 4 x 540s = 2160s."""
        from scrapy_impersonate.parser import RequestParser
        s = self._spider()
        probes = [r for r in s.parse_robots(self._robots())
                  if isinstance(r, Request) and r.url.endswith(".xml")]
        assert probes
        for r in probes:
            assert r.meta["download_timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
            assert RequestParser(r).as_dict()["timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT

    def test_the_probe_redirect_follow_carries_the_budget(self):
        s = self._spider()
        resp = TextResponse(
            url="https://example.com/sitemap.xml", body=b"", status=301,
            headers={"Location": b"https://example.com/sitemaps/sitemap.xml"},
            request=Request("https://example.com/sitemap.xml"),
        )
        resp.meta.update({"guessed_source": True, "probe_hops": 0})
        hop = next(r for r in s.parse_sitemap_probe(resp) if isinstance(r, Request))
        assert hop.meta["download_timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
        assert hop.meta["probe_hops"] == 1

    def test_the_retry_margin_is_pinned_not_inherited(self):
        """`--impersonate` sets RETRY_TIMES: 1, so the gate's margin used to depend on a CLI
        flag. RetryMiddleware prefers the meta key, so robots keeps 3 attempts either way."""
        s = self._spider()
        seed = next(iter(s._seed_requests()))
        assert seed.meta["max_retry_times"] == WebsiteSpider.ROBOTS_MAX_RETRY_TIMES
        from scrapy.downloadermiddlewares.retry import get_retry_request
        assert WebsiteSpider.ROBOTS_MAX_RETRY_TIMES == 2  # 3 attempts total
        assert get_retry_request  # imported: the meta key this relies on is a real API

    def test_pages_get_none_of_it(self):
        """The budget is sized for 1KB of text and must not leak onto real pages."""
        from scrapy.downloadermiddlewares.downloadtimeout import DownloadTimeoutMiddleware
        s = self._spider()
        list(s.parse_robots(self._robots()))
        page = next(iter(s._schedule("https://example.com/a-page/")))
        assert "max_retry_times" not in page.meta
        assert "impersonate_args" not in page.meta
        DownloadTimeoutMiddleware(180).process_request(page)
        assert page.meta["download_timeout"] == 180

    def test_the_budget_survives_a_jobdir_pickle_round_trip(self):
        """The robots request is re-seeded every session, but a redirect hop can be sitting
        in the frontier at interrupt time and IS persisted. Round-tripped through Scrapy's
        real serialization, not a hand-rolled copy."""
        import pickle
        s = self._spider()
        seed = next(iter(s._seed_requests()))
        restored = pickle.loads(pickle.dumps(seed.to_dict(spider=s)))
        assert restored["meta"]["download_timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
        assert restored["meta"]["impersonate_args"] == {
            "timeout": WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT}
        assert restored["meta"]["max_retry_times"] == WebsiteSpider.ROBOTS_MAX_RETRY_TIMES

    def test_the_budget_survives_a_retry(self):
        """RetryMiddleware rebuilds the request with `.copy()`; if the meta did not survive
        that, attempts 2 and 3 would silently go back to 180s with a green suite."""
        s = self._spider()
        seed = next(iter(s._seed_requests()))
        again = seed.copy()
        assert again.meta["download_timeout"] == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
        assert again.meta["max_retry_times"] == WebsiteSpider.ROBOTS_MAX_RETRY_TIMES

    def test_impersonate_args_is_inert_when_not_impersonating(self):
        """It rides along on every robots request regardless of `--impersonate`, so it has to
        be a no-op on the default path. scrapy-impersonate's handler only consults it when
        `meta['impersonate']` is set, which the middleware does not do when off."""
        from scrapy_impersonate.handler import ImpersonateDownloadHandler
        import inspect
        src = inspect.getsource(ImpersonateDownloadHandler.download_request)
        assert 'request.meta.get("impersonate")' in src, (
            "the impersonate handler no longer gates on meta['impersonate']; "
            "impersonate_args may no longer be inert on the default path"
        )
        s = self._spider()
        seed = next(iter(s._seed_requests()))
        assert "impersonate" not in seed.meta


class TestAllowAllIsVisible:
    """#82 review: proceeding allow-all is the highest-stakes thing this crawler does
    silently, so the one line that reports it must actually say something."""

    def _spider(self):
        s = WebsiteSpider(domain="example.com")
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_scrapys_wrapped_exceptions_carry_no_message(self):
        """The reason the log needed fixing, pinned against the real classes. Every wrapper
        stringifies EMPTY, so the warning rendered as `could not be fetched ()`. It went
        unnoticed because the existing test passes an OSError, which renders fine."""
        from scrapy.exceptions import (CannotResolveHostError, DownloadTimeoutError,
                                       DownloadConnectionRefusedError)
        for cls in (DownloadTimeoutError, CannotResolveHostError,
                    DownloadConnectionRefusedError):
            assert str(cls()) == "", cls.__name__
        assert str(OSError("boom")) == "boom"

    def test_the_warning_names_the_exception_type(self, caplog):
        import logging
        from scrapy.exceptions import DownloadTimeoutError
        s = self._spider()
        with caplog.at_level(logging.WARNING):
            list(s.robots_failed(types.SimpleNamespace(value=DownloadTimeoutError())))
        assert "DownloadTimeoutError" in caplog.text
        assert "could not be fetched ()" not in caplog.text

    def test_a_message_bearing_exception_keeps_its_message(self, caplog):
        import logging
        s = self._spider()
        with caplog.at_level(logging.WARNING):
            list(s.robots_failed(types.SimpleNamespace(value=OSError("dns is down"))))
        assert "dns is down" in caplog.text
        assert "OSError" in caplog.text


class TestRobotsTimeoutOverride:
    """Issue #92: the robots.txt budget gets an operator knob, mirroring the crawl-delay one
    beside it -- but RAISE-ONLY, which is the whole design.

    A SHORTER budget does not make crawls snappier. Timing out routes to `robots_failed`,
    which proceeds ALLOW-ALL -- so tuning this down makes a slow site look like one with no
    robots.txt and crawls it against a Disallow it never managed to read. The knob exists for
    the opposite case: a genuinely slow host an operator wants to wait longer for."""

    def _spider(self, domain="example.com"):
        s = WebsiteSpider(domain=domain)
        s.crawler = types.SimpleNamespace(stats=_FakeStats())
        return s

    def test_unset_uses_the_default(self, monkeypatch):
        monkeypatch.delenv("YOKO_CRAWL_ROBOTS_TIMEOUT", raising=False)
        s = self._spider()
        assert s.robots_download_timeout == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT

    def test_a_higher_value_is_honoured_end_to_end(self, monkeypatch):
        """Through the REAL consumers, not just the attribute -- the same discipline #82
        needed, since the two download paths read different keys."""
        from scrapy.downloadermiddlewares.downloadtimeout import DownloadTimeoutMiddleware
        from scrapy_impersonate.parser import RequestParser
        monkeypatch.setenv("YOKO_CRAWL_ROBOTS_TIMEOUT", "180")
        s = self._spider()
        assert s.robots_download_timeout == 180
        seed = next(iter(s._seed_requests()))
        DownloadTimeoutMiddleware(180).process_request(seed)
        assert seed.meta["download_timeout"] == 180
        assert RequestParser(seed).as_dict()["timeout"] == 180

    def test_a_lower_value_is_refused_not_honoured(self, monkeypatch, caplog):
        """The load-bearing one. Accepting 5s here would silently trade robots obedience for
        speed on exactly the sites least able to answer quickly."""
        import logging
        monkeypatch.setenv("YOKO_CRAWL_ROBOTS_TIMEOUT", "5")
        with caplog.at_level(logging.WARNING):
            s = self._spider()
        assert s.robots_download_timeout == WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT
        assert "IGNORED" in caplog.text
        assert "allow-all" in caplog.text, "the log must say WHY, not just that it clamped"

    def test_zero_and_negative_cannot_disable_the_bound(self, monkeypatch):
        """`0` reads as "no timeout" in several libraries; here it must not disable the gate."""
        for raw in ("0", "-1", "-999"):
            monkeypatch.setenv("YOKO_CRAWL_ROBOTS_TIMEOUT", raw)
            assert self._spider().robots_download_timeout == \
                WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT, raw

    def test_junk_falls_back_loudly_rather_than_crashing_the_crawl(self, monkeypatch, caplog):
        import logging
        for raw in ("", "  ", "abc", "60s", "nan"):
            monkeypatch.setenv("YOKO_CRAWL_ROBOTS_TIMEOUT", raw)
            with caplog.at_level(logging.WARNING):
                s = self._spider()
            assert s.robots_download_timeout == \
                WebsiteSpider.DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT, raw

    def test_a_float_string_is_accepted_as_a_whole_number_of_seconds(self, monkeypatch):
        monkeypatch.setenv("YOKO_CRAWL_ROBOTS_TIMEOUT", "90.5")
        assert self._spider().robots_download_timeout == 90

    def test_the_knob_matches_its_sibling_in_shape(self, monkeypatch):
        """#92's actual complaint was the ASYMMETRY -- one robots knob env-tunable, the other
        not. Both are now resolved per-instance from the environment with a DEFAULT_ prefix
        on the class constant, so neither reads as the odd one out."""
        assert hasattr(WebsiteSpider, "DEFAULT_MAX_ROBOTS_CRAWL_DELAY")
        assert hasattr(WebsiteSpider, "DEFAULT_ROBOTS_DOWNLOAD_TIMEOUT")
        monkeypatch.setenv("YOKO_CRAWL_MAX_ROBOTS_DELAY", "20")
        monkeypatch.setenv("YOKO_CRAWL_ROBOTS_TIMEOUT", "120")
        s = self._spider()
        assert s.max_robots_crawl_delay == 20.0
        assert s.robots_download_timeout == 120
