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
