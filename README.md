# yoko-crawler

A lightweight Python API service that crawls websites and returns discovered URLs as NDJSON. Built with FastAPI and Scrapy.

## What it does

Send a domain, get back every URL on the site — with HTTP status codes, redirect targets, and referrer information. The service handles the crawl asynchronously: you start a job, poll for progress, and stream the results when it's done.

```
POST /crawl        → start a crawl, get a job ID
GET  /crawl/{id}   → check progress (urls discovered, urls crawled)
GET  /crawl/{id}/results → stream NDJSON results
DELETE /crawl/{id} → cancel or clean up
GET  /health       → service status
```

## Architecture

```
  Reverse proxy (Caddy or nginx)
    → TLS termination
    → Rate limiting
           │
           ▼
  FastAPI (uvicorn, single worker)
    → Bearer token auth
    → Domain validation + SSRF prevention
    → Job management (max 3 concurrent)
           │
           ▼
  Scrapy Subprocesses (up to 3)
    → asyncio.create_subprocess_exec
    → Atomic status file IPC (every 3s)
    → NDJSON output streamed back via API
```

Each crawl runs as an isolated subprocess. If Scrapy crashes or hits its memory limit, the API stays up. Progress is tracked via atomic JSON status files — no shared memory, no message queues, no database.

## Quick start (local development)

```bash
# Install dependencies
pip install -r requirements.txt

# Set API key and run
export YOKO_CRAWL_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
uvicorn main:app --port 8100

# Test
curl http://localhost:8100/health
curl -X POST http://localhost:8100/crawl \
  -H "Authorization: Bearer $YOKO_CRAWL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain": "example.com"}'
```

The `POST /crawl` body accepts optional `impersonate` and `delay` — the two knobs for WAF-protected sites:

```bash
curl -X POST http://localhost:8100/crawl \
  -H "Authorization: Bearer $YOKO_CRAWL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain": "example.com", "impersonate": "chrome", "delay": 3}'
```

## API reference

All `/crawl` routes require `Authorization: Bearer $YOKO_CRAWL_API_KEY`.

### `POST /crawl`

Request body:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `domain` | string | — | Required. Bare hostname; protocol/path/port are stripped. |
| `impersonate` | enum | `off` | `off`, `chrome`, `firefox`, `safari`, or `random`. Browser TLS fingerprint for Cloudflare-protected sites. |
| `delay` | number | `1` | Seconds between requests, `0`–`30`. Try `3`–`5` for aggressive WAFs (the API caps at 30; the CLI `--delay` is unbounded). |
| `profile` | enum | `standard` | `standard` or `presale`. `presale` is a politer bundle (serial, ≥3s delay) for prospect sites you don't control. Never relaxes SSRF/domain validation. |
| `emit_content` | bool | `false` | When `true`, each HTML page's extracted main-content text is included in a `content_text` field. Off keeps output lean; the content hash and structural counts are emitted regardless. |
| `cookies` | string | — | Raw `Cookie`-header string (`cf_clearance=…; __cf_bm=…`, ≤8192 chars) sent with every request via the cookie jar. Reuse a browser-solved Cloudflare clearance cookie when even `impersonate` is blocked (a JS/managed challenge). Pair with `user_agent`. **Caveat:** `cf_clearance` is bound to the User-Agent **and usually the IP** that solved the challenge — a cookie solved in your browser (your IP) is rejected from the crawler's (different) IP unless the site's Cloudflare doesn't bind the bypass cookie to IP. |
| `user_agent` | string | — | Override the `User-Agent` on every request (≤512 chars). Required alongside a `cf_clearance` cookie so the UA matches the one that solved the challenge; also survives `impersonate` (pass it only to deliberately override the impersonated browser's UA). |

Response `202`:

```json
{"job_id": "a1b2c3d4e5f60718", "status": "running", "impersonate": "chrome", "delay": 3.0, "profile": "standard", "emit_content": false, "message": "Crawl queued for example.com"}
```

Other status codes: `409` (domain already crawling), `429` (concurrency limit), `422` (validation).

### `GET /crawl/{id}`

Returns job status, including `impersonate`, `delay`, `profile`, and `emit_content`, plus `urls_discovered`/`urls_crawled`. A crawl where every host was SSRF-blocked (nothing fetched) is reported as `failed` with an explanatory `error`. A wholesale bot-block (all-403) is **not** failed — the crawl `completed`s and emits its `403` rows so the consumer (yoko-corpus) can retry with impersonation and/or present an honest "we couldn't read this site" report; the `waf_challenge_count` stat records recognized Cloudflare/WAF challenge pages (which are emitted but not mined for content or followed).

### `GET /crawl/{id}/results`

Streams NDJSON once the job is `completed`.

### Errors

All error responses use a flat envelope: `{"detail": "<message>"}` (multiple field errors are joined). Example for an out-of-range `delay`: `{"detail": "delay: Input should be less than or equal to 30"}`.

## Deployment

Two deployment options:

- **[VPS with Caddy + systemd](docs/vps-deployment.md)** — Deploy directly on Ubuntu with Caddy for TLS. Simpler, no containerization overhead.
- **[Docker with nginx](docs/deployment-checklist.md)** — Containerized deployment with nginx reverse proxy. More isolated, portable.

## Output format

Via the API, results are streamed as [NDJSON](http://ndjsonspec.org/) (one JSON object per line). For local/one-off crawls, you can output CSV directly:

```bash
python run_spider.py --domain example.com --output results.csv --status-file /dev/null --format csv
```

Supported formats: `jsonlines` (default), `csv`.

If the target site has an aggressive WAF (e.g. Wordfence), use `--delay` to slow down:

```bash
python run_spider.py --domain example.com --output results.csv --status-file /dev/null --format csv --delay 5
```

At `--delay 3` or higher, the crawler switches to serial mode (one request at a time) to avoid triggering rate limits. Default is `1`.

For prospect sites you don't control (and have permission to crawl), use the politer **presale** profile instead of tuning the delay by hand — it forces serial mode with a ≥3s delay:

```bash
python run_spider.py --domain example.com --output results.csv --status-file /dev/null --format csv --profile presale
```

Permission to crawl a prospect's site is an operational/legal matter handled by humans; the profile is only a politeness setting and never relaxes SSRF/domain validation.

### Cloudflare and other TLS-fingerprinting WAFs

Some sites (notably those behind **Cloudflare Bot Management**) block on the
**TLS handshake fingerprint** (JA3/JA4), not the User-Agent or headers. Standard
Scrapy — like Python `requests` — gets a `403` no matter what UA you send, while
`curl`/`wget` over HTTP/1.1 pass. `--delay` and `--user-agent` will *not* help
here; the block is below the HTTP layer.

For these sites, use `--impersonate` to present a real browser's TLS fingerprint
(via [scrapy-impersonate](https://pypi.org/project/scrapy-impersonate/) /
`curl_cffi`):

```bash
python run_spider.py --domain example.com --output results.csv --status-file /dev/null --format csv --impersonate chrome
```

Choices: `off` (default — standard Scrapy TLS), `chrome`, `firefox`, `safari`,
or `random` (rotate across the current set). Pinned to current browser versions
in `tls_impersonate.py`; bump those as `curl_cffi` ships newer targets.

When impersonating, each request is sent with a User-Agent matching its TLS
fingerprint (so UA and JA3 stay consistent across `chrome`/`firefox`/`safari`,
including `random`). Pass `--user-agent` only to deliberately override it.

> **How to tell which you need:** if a plain crawl returns `403` but
> `wget --user-agent="…<chrome UA>…" https://site/` returns `200`, it's
> TLS fingerprinting — use `--impersonate chrome`.

Some sites block the deployed crawler's **datacenter IP** regardless — impersonation and a
real headless browser are both refused, while the same crawl from a residential connection
works. For those, run the crawl on your own machine with `scripts/local_scrape.sh` and hand
the NDJSON to the corpus: see the
**[local-scrape runbook](docs/local-scrape-runbook.md)** for the full flow. The durable fix
— a self-hosted trusted-IP proxy so the droplet can crawl these itself — is
[issue #22](https://github.com/Yoko-Co/yoko-crawler/issues/22).

### Sample row

```json
{"url": "https://example.com/about", "status": 200, "last_modified": "", "redirected_to": "", "referrer": "https://example.com/", "content_hash": "9f86d0…", "main_content_extracted": true, "word_count": 412, "link_count": 18, "internal_link_count": 15, "external_link_count": 3, "pdf_link_count": 2, "asset_link_count": 2, "anchor_link_count": 0, "image_count": 4, "table_count": 0, "form_count": 1, "iframe_count": 1, "heading_count": 6, "embed_count_nonbenign": 0, "component_count": 2, "iframe_hosts": ["www.youtube.com"], "canonical": "https://example.com/about"}
```

### Original fields (unchanged)

- **url** — the discovered URL (normalized, deduped)
- **status** — HTTP status code the site returned
- **redirected_to** — redirect target (for 3xx responses)
- **referrer** — the first page that linked to this URL
- **last_modified** — Last-Modified header value, if present

### Content & structural enrichment (additive)

These fields are present on **every** row. For non-HTML rows (assets fetched HEAD-only, non-HTML responses, redirects) they carry zero/empty defaults (`content_hash: ""`, counts `0`, `iframe_hosts: []`, `main_content_extracted: false`). For HTML pages the counts are scoped to the **main content region** (nav/header/footer excluded) when extraction succeeds.

- **content_hash** — SHA-256 (hex) of the page's normalized main-content text, for change detection. Empty string for non-HTML rows.
- **main_content_extracted** — `true` when the counts are scoped to a located main region; `false` when they fall back to the `<body>` (or the row is non-HTML). On the fallback, site chrome (`<nav>`/`<aside>`/`<header>`/`<footer>` tags and navigation/banner/contentinfo/search ARIA roles) is excluded from the counts (issue #9) so the nav bar and per-page search box don't inflate word/link/form counts. Two guards keep it from eating real content (a zeroed page would read falsely simple): chrome inside an `<article>` is kept (that article's own title/byline/TOC), and any chrome block that actually holds content — an `<article>`/`<main>` descendant or substantial non-link prose — is kept. Known limitation: non-semantic chrome (`<div class="menu">`) has no tag/role signal and is not stripped.
- **canonical** — the page's `<link rel="canonical">` target, resolved to absolute and normalized like any URL (issue #10; `""` when absent). Lets the corpus collapse query-string/pagination/variant URLs onto their canonical page.
- **word_count**, **heading_count** — words and `<h1>`–`<h6>` in the main content.
- **link_count**, **internal_link_count**, **external_link_count** — `<a href>` links, split by the spider's internal/external rule.
- **pdf_link_count**, **asset_link_count** — links whose target ends in `.pdf` / any known asset extension (`.pdf` counts as both).
- **anchor_link_count** — in-page jump links (`#frag`, or a link resolving to the current page plus a fragment).
- **image_count**, **table_count**, **form_count**, **iframe_count** — `<img>`/`<table>`/`<form>`/`<iframe>` in the main content.
- **embed_count_nonbenign** — iframes whose host is **not** on the benign-embed allowlist (the "surprise embed" signal: Tableau, data dashboards, unknown hosts — excludes routine video/map embeds). Computed page-wide (header/footer/sidebar embeds count, not just main content). Allowlist-relative — it can change across crawls if the allowlist changes; `iframe_hosts` is the durable signal for cross-crawl comparison.
- **component_count** — count of interactive JS components (sliders/carousels/accordions/tabs/galleries/lightboxes) detected by container markers (issue #12). Real design+dev work otherwise invisible (JS-hydrated) or laundered into word/image counts.
- **iframe_hosts** — distinct hostnames of all `<iframe src>`. The durable raw signal; downstream consumers can re-classify it even if the allowlist changes. A real JSON array in `jsonlines`; JSON-encoded into a string for `csv`.
- **content_text** — the extracted main-content text. **Present only when `--emit-content` / `emit_content: true` is set.** Its absence means "not requested," not "empty."

**Hash always, text on demand.** The content hash and structural counts are emitted on every crawl so change detection stays cheap; the full `content_text` is large and only included on request (`--emit-content`), keeping the default output lean for existing consumers. The hash is computed over the **same** normalized text whether or not `--emit-content` is set, so a content-only crawl and a full crawl produce identical hashes for an unchanged page.

**Normalization (stable across runs).** `content_hash` and `content_text` use the same fixed normalization: Unicode NFC → normalize line endings → collapse all whitespace runs to one space → strip. Case is preserved. The extraction library (`trafilatura`) is pinned exactly in `requirements.txt` because its heuristics change between versions; a deliberate upgrade is a "hash-epoch" change that re-hashes unchanged pages.

When main-content extraction fails, `content_hash` is computed over the normalized `<body>` text instead of the main text, and `main_content_extracted` is `false`. The hashed scope can therefore flip between crawls if extraction succeeds on one run and fails on another — gate cross-crawl change detection on `main_content_extracted` when that matters. Oversized pages (over the internal body-size guard) emit an empty `content_hash` and zero counts.

> **Backward compatibility.** All enrichment fields are additive — the original five fields keep their names and types. Existing consumers that read known keys (the Yoko 301s importer reads only the keys it needs, with null-coalescing defaults) are unaffected; `content_text`'s conditional presence is the only schema variation.

## Spider features

The bundled spider (`website_spider.py`) does comprehensive URL discovery:

- Seeds from robots.txt sitemaps, then follows all internal links
- Records HTTP status, redirect targets, and referrers
- Handles pagination archives (traverses without recording each page URL)
- Skips login/auth URLs (wp-login, OAuth, SSO, SAML, etc.)
- Skips non-navigational hrefs (`mailto:`/`tel:`/`javascript:`/`data:`…) — including malformed ones like `mail to:` that would otherwise be resolved into a crawlable path (issue #11)
- Issues HEAD requests for non-HTML assets (PDFs, images, etc.)
- Normalizes URLs and strips non-content query params — tracking (UTM, session IDs, etc.) and on-site search/comment params (`?s=`, `replytocom`) — so query-only variants of the same page are deduped and not re-crawled (issue #8)
- Contains **faceted search** (issue #49) — a multi-select filter UI fans out combinatorially, since every filter *subset* is a URL and every *ordering* of a subset is another URL. Two guards: indexed facet params (`f[0]`, `tid[2]`) are deduped **order-insensitively**, so the many orderings of one selection are fetched once; and selections deeper than `max_facet_depth` (default 2) are skipped as duplicate views of a result set. Only facet-*shaped* params are affected — an identity param (`?id=5`, `?product=hat`) never trips either guard, so a query-param product catalog still crawls in full. On naeyc.org this cut 1,921 requests to 435, of which the runaway search page dropped from 1,491 to 5
- Respects autothrottle for polite crawling
- Captures per-page main-content text, structural counts, a change-detection content hash, and surprise-embed signals (see [Output format](#output-format))

## Security

- **Auth**: Bearer token with constant-time comparison (`secrets.compare_digest`)
- **SSRF prevention**: Three-layer defense — domain format validation, async DNS resolution against blocked networks (RFC 1918, link-local, cloud metadata, IPv4-mapped IPv6, 6to4, Teredo), and Scrapy DNS cache pinning
- **Reverse proxy**: Caddy (automatic TLS) or nginx (manual TLS) for rate limiting and security headers
- **Docker hardening** (when containerized): Non-root user, `cap_drop: ALL`, `read_only: true`, `no-new-privileges`

## Configuration

All configuration is via environment variables and hardcoded defaults:

| Variable | Required | Description |
|----------|----------|-------------|
| `YOKO_CRAWL_API_KEY` | Yes | Bearer token (minimum 32 characters) |
| `YOKO_CRAWL_RESULTS_DIR` | No | Path for result files (default: `/opt/yoko-crawl/results`) |
| `YOKO_CRAWL_BENIGN_EMBEDS` | No | Comma-separated extra hosts to treat as benign iframe embeds (added to the built-in allowlist in `embed_allowlist.py`). Matched by suffix, so a bare domain covers its subdomains. |

Spider settings are hardcoded in `run_spider.py` for the intended use case:
- Max crawl duration: 2 hours
- Max URLs: 50,000
- Memory limit per spider: 384 MB
- Max concurrent crawls: 3

## Development

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run tests
pytest tests/ -v

# Run locally (set the env var first)
export YOKO_CRAWL_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
uvicorn main:app --port 8000
```

## Project structure

```
main.py              # FastAPI app, routes, lifespan
job_manager.py       # Job lifecycle, subprocess management, cleanup
domain_validator.py  # SSRF prevention (format + DNS + range checks)
auth.py              # Bearer token auth dependency
run_spider.py        # Subprocess entry point (configures Scrapy)
stats_extension.py   # Scrapy extension (writes progress to status file)
tls_impersonate.py   # Downloader middleware: browser TLS fingerprint (curl_cffi)
content_extractor.py # Main-content extraction, structural counts, embed signals, content hash
embed_allowlist.py   # Configurable benign-embed allowlist (surprise-embed signal)
website_spider.py    # The actual crawler
Dockerfile           # python:3.13-slim-bookworm, non-root user
docker-compose.yml   # Memory limits, healthcheck, security hardening
nginx/               # Reverse proxy config (TLS, rate limiting)
tests/               # pytest-asyncio + httpx
```

## License

MIT
