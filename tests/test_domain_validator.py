"""Tests for domain_validator.py."""

import ipaddress
from unittest.mock import AsyncMock, patch

import pytest

from domain_validator import (
    DomainValidationError,
    _is_blocked,
    validate_domain,
    validate_domain_format,
)


class TestValidateDomainFormat:
    def test_valid_domain(self):
        assert validate_domain_format("example.com") == "example.com"

    def test_strips_protocol(self):
        assert validate_domain_format("https://example.com") == "example.com"
        assert validate_domain_format("http://example.com") == "example.com"

    def test_strips_path(self):
        assert validate_domain_format("example.com/page") == "example.com"

    def test_strips_port(self):
        assert validate_domain_format("example.com:8080") == "example.com"

    def test_lowercases(self):
        assert validate_domain_format("EXAMPLE.COM") == "example.com"

    def test_strips_whitespace(self):
        assert validate_domain_format("  example.com  ") == "example.com"

    def test_rejects_empty(self):
        with pytest.raises(DomainValidationError):
            validate_domain_format("")

    def test_rejects_ip_address(self):
        with pytest.raises(DomainValidationError, match="IP addresses"):
            validate_domain_format("192.168.1.1")

    def test_rejects_ipv6(self):
        with pytest.raises(DomainValidationError):
            validate_domain_format("::1")

    def test_rejects_too_long(self):
        with pytest.raises(DomainValidationError, match="character limit"):
            validate_domain_format("a" * 250 + ".com")

    def test_rejects_invalid_format(self):
        with pytest.raises(DomainValidationError, match="Invalid domain"):
            validate_domain_format("not a domain!")

    def test_allows_subdomains(self):
        assert validate_domain_format("www.example.com") == "www.example.com"
        assert validate_domain_format("sub.domain.example.com") == "sub.domain.example.com"

    def test_rejects_single_label(self):
        with pytest.raises(DomainValidationError):
            validate_domain_format("localhost")


class TestIsBlocked:
    def test_private_ipv4(self):
        assert _is_blocked(ipaddress.ip_address("10.0.0.1"))
        assert _is_blocked(ipaddress.ip_address("172.16.0.1"))
        assert _is_blocked(ipaddress.ip_address("192.168.1.1"))

    def test_loopback(self):
        assert _is_blocked(ipaddress.ip_address("127.0.0.1"))

    def test_link_local(self):
        assert _is_blocked(ipaddress.ip_address("169.254.169.254"))

    def test_public_ip_allowed(self):
        assert not _is_blocked(ipaddress.ip_address("8.8.8.8"))
        assert not _is_blocked(ipaddress.ip_address("1.1.1.1"))

    def test_ipv4_mapped_ipv6(self):
        # ::ffff:169.254.169.254 should be blocked.
        assert _is_blocked(ipaddress.ip_address("::ffff:169.254.169.254"))

    def test_ipv6_loopback(self):
        assert _is_blocked(ipaddress.ip_address("::1"))


class TestValidateDomain:
    async def test_valid_domain_resolves(self):
        mock_results = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]
        with patch("domain_validator.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.getaddrinfo = AsyncMock(return_value=mock_results)
            result = await validate_domain("example.com")
            assert result == "example.com"

    async def test_rejects_private_resolution(self):
        mock_results = [
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ]
        with patch("domain_validator.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.getaddrinfo = AsyncMock(return_value=mock_results)
            with pytest.raises(DomainValidationError, match="private or reserved"):
                await validate_domain("example.com")

    async def test_rejects_unresolvable(self):
        import socket

        with patch("domain_validator.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.getaddrinfo = AsyncMock(
                side_effect=socket.gaierror("Name resolution failed")
            )
            with pytest.raises(DomainValidationError, match="does not resolve"):
                await validate_domain("nonexistent.invalid")
