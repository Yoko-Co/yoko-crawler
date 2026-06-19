"""Tests for the configurable benign-embed allowlist (U3)."""

from __future__ import annotations

from embed_allowlist import (
    DEFAULT_BENIGN_EMBED_HOSTS,
    is_benign_host,
    load_benign_hosts,
)


class TestIsBenignHost:
    def test_exact_default_host(self):
        assert is_benign_host("youtube.com", DEFAULT_BENIGN_EMBED_HOSTS)

    def test_subdomain_matches_by_suffix(self):
        assert is_benign_host("www.youtube.com", DEFAULT_BENIGN_EMBED_HOSTS)
        assert is_benign_host("player.vimeo.com", DEFAULT_BENIGN_EMBED_HOSTS)

    def test_non_benign_host(self):
        assert not is_benign_host("public.tableau.com", DEFAULT_BENIGN_EMBED_HOSTS)

    def test_trailing_dot_is_ignored(self):
        assert is_benign_host("youtube.com.", DEFAULT_BENIGN_EMBED_HOSTS)

    def test_lookalike_is_not_a_subdomain(self):
        # notyoutube.com must not match youtube.com.
        assert not is_benign_host("notyoutube.com", DEFAULT_BENIGN_EMBED_HOSTS)


class TestLoadBenignHosts:
    def test_defaults_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("YOKO_CRAWL_BENIGN_EMBEDS", raising=False)
        assert load_benign_hosts() == DEFAULT_BENIGN_EMBED_HOSTS

    def test_env_adds_hosts(self, monkeypatch):
        monkeypatch.setenv("YOKO_CRAWL_BENIGN_EMBEDS", "public.tableau.com, datawrapper.de")
        hosts = load_benign_hosts()
        assert "public.tableau.com" in hosts
        assert "datawrapper.de" in hosts
        # Defaults are preserved (additive, not replace).
        assert "youtube.com" in hosts

    def test_blank_env_yields_defaults(self, monkeypatch):
        monkeypatch.setenv("YOKO_CRAWL_BENIGN_EMBEDS", "   ")
        assert load_benign_hosts() == DEFAULT_BENIGN_EMBED_HOSTS
