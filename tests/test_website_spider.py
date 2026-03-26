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
