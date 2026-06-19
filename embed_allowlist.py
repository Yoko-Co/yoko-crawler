"""
Benign-embed allowlist for the crawler's "surprise embed" signal.

An iframe whose host is on this allowlist is a routine embed (video, basic map)
and does not count toward ``embed_count_nonbenign``. Everything else -- Tableau,
data dashboards, unknown hosts -- is non-benign by default, because those are the
embeds that cause deployment surprises during a migration.

The list is expected to be tuned over time. It is intentionally small and
explicit. Matching is by host suffix, so a bare domain here also covers its
subdomains (``youtube.com`` matches ``www.youtube.com``).

Override (additive) via the ``YOKO_CRAWL_BENIGN_EMBEDS`` environment variable: a
comma-separated list of hosts, added to the defaults below.
"""

from __future__ import annotations

import os

# Bare domains; subdomains are matched by suffix (see is_benign_host).
# Google is intentionally NOT allowlisted at the bare-domain level: bare
# "google.com" would suffix-match data-dashboard subdomains
# (lookerstudio.google.com, datastudio.google.com) -- exactly the "surprise
# embeds" we want to flag. Only the basic Maps embed hosts are benign.
DEFAULT_BENIGN_EMBED_HOSTS = frozenset(
    {
        "youtube.com",  # also covers www.youtube.com
        "youtu.be",
        "youtube-nocookie.com",  # also covers www.youtube-nocookie.com
        "vimeo.com",  # also covers player.vimeo.com
        "www.google.com",  # basic Google Maps embeds (www.google.com/maps)
        "maps.google.com",
    }
)

_ENV_VAR = "YOKO_CRAWL_BENIGN_EMBEDS"


def load_benign_hosts() -> frozenset[str]:
    """Return the active benign-embed host set: the defaults plus any hosts from
    the ``YOKO_CRAWL_BENIGN_EMBEDS`` env var (additive, comma-separated).
    """
    raw = os.environ.get(_ENV_VAR, "")
    extra = {h.strip().lower() for h in raw.split(",") if h.strip()}
    if not extra:
        return DEFAULT_BENIGN_EMBED_HOSTS
    return DEFAULT_BENIGN_EMBED_HOSTS | extra


def is_benign_host(host: str, benign_hosts: frozenset[str]) -> bool:
    """True when ``host`` equals or is a subdomain of any allowlisted host."""
    host = host.lower().rstrip(".")
    return any(host == b or host.endswith(f".{b}") for b in benign_hosts)
