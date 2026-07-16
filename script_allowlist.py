"""
Benign-script allowlist for the crawler's "third-party integration" signal (issue #28).

A ``<script src>`` whose host is on this allowlist is routine infrastructure --
analytics, tag managers, common CDNs, web fonts -- and does NOT count toward
``script_embed_count_nonbenign``. Everything else -- chat widgets, forms-as-a-
service, booking, donation, CRM/marketing tags, social embeds -- is non-benign by
default, because those are the integrations that cause deployment surprises during
a migration.

This is DELIBERATELY separate from embed_allowlist (iframes): the routine hosts for
scripts are completely different from the routine hosts for iframes. Reusing the
iframe list here would flag Google Analytics, jQuery, and Google Fonts as surprise
integrations -- noise that would swamp the real signal.

The list is a first-pass best guess, expected to be tuned against real crawls.
Matching is by host suffix (see is_benign_host), so a bare domain also covers its
subdomains. Override (additive) via ``YOKO_CRAWL_BENIGN_SCRIPTS``: a comma-separated
list of hosts, added to the defaults below.
"""

from __future__ import annotations

import os

# Reuse the generic host-suffix matcher; only the host SET differs from iframes.
from embed_allowlist import is_benign_host  # noqa: F401 (re-exported for callers)

# Bare domains; subdomains are matched by suffix. Curated as "routine web plumbing
# that migrates trivially", NOT integrations. Unknown hosts are non-benign by design.
DEFAULT_BENIGN_SCRIPT_HOSTS = frozenset(
    {
        # Analytics / tag management (routine measurement, not an integration to rebuild)
        "google-analytics.com",
        "googletagmanager.com",
        "googleoptimize.com",
        "googleadservices.com",
        "gstatic.com",  # Google static assets (also covers www.gstatic.com)
        "www.google.com",  # reCAPTCHA loader (www.google.com/recaptcha/api.js) -- ubiquitous
        # Common script/CDN hosts
        "ajax.googleapis.com",
        "code.jquery.com",
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "bootstrapcdn.com",  # covers maxcdn./stackpath.bootstrapcdn.com
        # NOTE: polyfill.io is deliberately NOT here -- the domain was sold and served malware
        # in the mid-2024 supply-chain incident, so it's exactly the kind of risky dependency a
        # migration crawl should surface, not silence.
        # Web fonts
        "fonts.googleapis.com",
        "use.typekit.net",
        "use.fontawesome.com",
        "kit.fontawesome.com",
        # WordPress core assets
        "s.w.org",
    }
)

_ENV_VAR = "YOKO_CRAWL_BENIGN_SCRIPTS"


def load_benign_script_hosts() -> frozenset[str]:
    """Return the active benign-script host set: the defaults plus any hosts from
    the ``YOKO_CRAWL_BENIGN_SCRIPTS`` env var (additive, comma-separated)."""
    raw = os.environ.get(_ENV_VAR, "")
    extra = {h.strip().lower() for h in raw.split(",") if h.strip()}
    if not extra:
        return DEFAULT_BENIGN_SCRIPT_HOSTS
    return DEFAULT_BENIGN_SCRIPT_HOSTS | extra
