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
