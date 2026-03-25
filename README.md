# yoko-crawler

A lightweight Python API service that crawls websites and returns discovered URLs as NDJSON. Built with FastAPI and Scrapy, deployed as a Docker container behind nginx.

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
┌──────────────────────────────────────────────┐
│  Docker Container                            │
│                                              │
│  FastAPI (uvicorn, single worker)            │
│    → Bearer token auth                       │
│    → Domain validation + SSRF prevention     │
│    → Job management (max 3 concurrent)       │
│                                              │
│  Scrapy Subprocesses (up to 3)               │
│    → asyncio.create_subprocess_exec          │
│    → Atomic status file IPC (every 3s)       │
│    → NDJSON output streamed back via API     │
│                                              │
│  nginx (TLS, rate limiting, security headers)│
└──────────────────────────────────────────────┘
```

Each crawl runs as an isolated subprocess. If Scrapy crashes or hits its memory limit, the API stays up. Progress is tracked via atomic JSON status files — no shared memory, no message queues, no database.

## Quick start

```bash
# Clone and configure
git clone <repo-url> && cd yoko-crawler
echo "YOKO_CRAWL_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" > .env

# Run
docker compose up -d --build

# Test
curl http://localhost:8100/health
curl -X POST http://localhost:8100/crawl \
  -H "Authorization: Bearer $(cat .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"domain": "example.com"}'
```

For production deployment with TLS and nginx, see [docs/deployment-checklist.md](docs/deployment-checklist.md).

## Output format

Results are streamed as [NDJSON](http://ndjsonspec.org/) (one JSON object per line):

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
- Issues HEAD requests for non-HTML assets (PDFs, images, etc.)
- Normalizes URLs and strips tracking parameters (UTM, session IDs, etc.)
- Respects autothrottle for polite crawling

## Security

- **Auth**: Bearer token with constant-time comparison (`secrets.compare_digest`)
- **SSRF prevention**: Three-layer defense — domain format validation, async DNS resolution against blocked networks (RFC 1918, link-local, cloud metadata, IPv4-mapped IPv6, 6to4, Teredo), and Scrapy DNS cache pinning
- **Docker hardening**: Non-root user, `cap_drop: ALL`, `read_only: true`, `no-new-privileges`
- **nginx**: TLS 1.2+, HSTS, rate limiting on POST, `proxy_buffering off` for streaming

## Configuration

All configuration is via environment variables and hardcoded defaults:

| Variable | Required | Description |
|----------|----------|-------------|
| `YOKO_CRAWL_API_KEY` | Yes | Bearer token (minimum 32 characters) |

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
website_spider.py    # The actual crawler
Dockerfile           # python:3.13-slim-bookworm, non-root user
docker-compose.yml   # Memory limits, healthcheck, security hardening
nginx/               # Reverse proxy config (TLS, rate limiting)
tests/               # pytest-asyncio + httpx
```

## License

MIT
