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

Response `202`:

```json
{"job_id": "a1b2c3d4e5f60718", "status": "running", "impersonate": "chrome", "delay": 3.0, "message": "Crawl queued for example.com"}
```

Other status codes: `409` (domain already crawling), `429` (concurrency limit), `422` (validation).

### `GET /crawl/{id}`

Returns job status, including `impersonate` and `delay`, plus `urls_discovered`/`urls_crawled`. A crawl that was blocked wholesale (impersonation fingerprint stale, or every host SSRF-blocked) is reported as `failed` with an explanatory `error`, not a clean `completed`.

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

```json
{"url": "https://example.com/", "status": 200, "last_modified": "", "redirected_to": "", "referrer": ""}
{"url": "https://example.com/about", "status": 200, "last_modified": "", "redirected_to": "", "referrer": "https://example.com/"}
{"url": "https://example.com/old-page", "status": 301, "last_modified": "", "redirected_to": "https://example.com/new-page", "referrer": "https://example.com/"}
```

Each record includes:
- **url** — the discovered URL (normalized, deduped)
- **status** — HTTP status code the site returned
- **redirected_to** — redirect target (for 3xx responses)
- **referrer** — the first page that linked to this URL
- **last_modified** — Last-Modified header value, if present

## Spider features

The bundled spider (`website_spider.py`) does comprehensive URL discovery:

- Seeds from robots.txt sitemaps, then follows all internal links
- Records HTTP status, redirect targets, and referrers
- Handles pagination archives (traverses without recording each page URL)
- Skips login/auth URLs (wp-login, OAuth, SSO, SAML, etc.)
- Issues HEAD requests for non-HTML assets (PDFs, images, etc.)
- Normalizes URLs and strips tracking parameters (UTM, session IDs, etc.)
- Respects autothrottle for polite crawling

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
website_spider.py    # The actual crawler
Dockerfile           # python:3.13-slim-bookworm, non-root user
docker-compose.yml   # Memory limits, healthcheck, security hardening
nginx/               # Reverse proxy config (TLS, rate limiting)
tests/               # pytest-asyncio + httpx
```

## License

MIT
