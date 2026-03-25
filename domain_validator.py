"""
Three-layer domain validation and SSRF prevention.

Layer 1: Format validation (regex, length, IP rejection)
Layer 2: Async DNS resolution with blocked network checks
Layer 3: Scrapy DNS cache pinning (configured in run_spider.py)
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket

_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}$"
)

_MAX_DOMAIN_LENGTH = 253

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("240.0.0.0/4"),
    # IPv6
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class DomainValidationError(Exception):
    """Raised when domain validation fails."""


def validate_domain_format(domain: str) -> str:
    """
    Layer 1: Validate and normalize domain format.

    Strips protocol prefixes, trailing slashes/paths, rejects IP addresses,
    validates against strict hostname regex, enforces length limit.

    Returns the cleaned domain string.
    """
    domain = domain.strip().lower()

    # Strip protocol prefix if user included it.
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]

    # Strip trailing slashes and paths.
    domain = domain.split("/")[0]

    # Strip port if present.
    domain = domain.split(":")[0]

    if not domain:
        raise DomainValidationError("Domain is required")

    if len(domain) > _MAX_DOMAIN_LENGTH:
        raise DomainValidationError(
            f"Domain exceeds {_MAX_DOMAIN_LENGTH} character limit"
        )

    # Reject raw IP addresses.
    try:
        ipaddress.ip_address(domain)
        raise DomainValidationError("IP addresses are not allowed, use a domain name")
    except ValueError:
        pass  # Not an IP, which is what we want.

    if not _DOMAIN_RE.match(domain):
        raise DomainValidationError(f"Invalid domain format: {domain}")

    return domain


def _extract_ips_to_check(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list:
    """Extract all IPv4 addresses embedded in an IPv6 address."""
    ips = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ips.append(ip.ipv4_mapped)
        if ip.sixtofour:
            ips.append(ip.sixtofour)
        if ip.teredo:
            # teredo returns (server_ip, client_ip)
            ips.extend(ip.teredo)
    return ips


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if any embedded IP falls in a blocked network."""
    for check_ip in _extract_ips_to_check(ip):
        for network in _BLOCKED_NETWORKS:
            if check_ip in network:
                return True
    return False


async def check_dns_resolution(domain: str) -> None:
    """
    Layer 2: Async DNS resolution with SSRF range checks.

    Resolves the domain and checks all returned addresses against blocked
    networks, including IPv4-mapped IPv6, 6to4, and Teredo addresses.
    """
    loop = asyncio.get_running_loop()
    try:
        results = await asyncio.wait_for(
            loop.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP),
            timeout=10,
        )
    except asyncio.TimeoutError:
        raise DomainValidationError(
            f"DNS resolution timed out for {domain}"
        )
    except socket.gaierror:
        raise DomainValidationError(
            f"Domain does not resolve: {domain}"
        )

    if not results:
        raise DomainValidationError(
            f"Domain does not resolve: {domain}"
        )

    for family, _, _, _, sockaddr in results:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked(ip):
            raise DomainValidationError(
                "Domain resolves to a private or reserved address"
            )


async def validate_domain(domain: str) -> str:
    """
    Full domain validation: format check + DNS resolution + SSRF check.

    Returns the cleaned domain string.
    """
    domain = validate_domain_format(domain)
    await check_dns_resolution(domain)
    return domain
