# Yoko Crawler - Agent Instructions

## Project

Yoko Crawler — a Python/FastAPI service that runs Scrapy spiders as subprocesses to crawl websites and return discovered URLs as NDJSON. It serves as the backend for the Yoko 301s WordPress plugin's "Crawl Site" feature.

## Architecture

- **FastAPI app** (`main.py`) — API endpoints, lifespan management, single uvicorn worker (in-memory state)
- **Job Manager** (`job_manager.py`) — in-memory job dict with asyncio.Lock, max 3 concurrent crawls
- **Scrapy subprocess** — each crawl runs via `asyncio.create_subprocess_exec`, writes JSONL results and atomic status.json
- **Progress extension** (`stats_extension.py`) — Scrapy extension that writes progress to a status file every 3s; also fails an impersonated crawl that was blocked on every request (all 403) so a stale TLS fingerprint surfaces instead of a clean empty result
- **Domain validator** (`domain_validator.py`) — SSRF prevention: format check + async DNS range-check at submit time, with a synchronous re-check at crawl-worker startup
- **SSRF guard** (`ssrf_guard.py`) — downloader middleware that re-resolves each request's host at fetch time and drops any resolving to a blocked/reserved range (covers both the default and curl_cffi download handlers)
- **TLS impersonation** (`tls_impersonate.py`) — Scrapy downloader middleware that tags each request with a current browser TLS fingerprint (via curl_cffi / `scrapy-impersonate`) plus a matching User-Agent, to defeat JA3/JA4 WAFs; `IMPERSONATE_CHOICES` is the single source of truth for the `--impersonate` CLI flag and the API field
- **Auth** (`auth.py`) — Bearer token via `secrets.compare_digest`

## Deployment

The service runs the same Python code in either environment:

### VPS (primary)
- App code: `/opt/yoko-crawl/app/`
- Virtualenv: `/opt/yoko-crawl/venv/`
- Result files: `/opt/yoko-crawl/results/`
- Reverse proxy: Caddy (automatic TLS)
- Process manager: systemd (`yoko-crawl.service`)
- Guide: `docs/vps-deployment.md`

### Docker (alternative)
- Reverse proxy: nginx (manual TLS via certbot)
- Result files: `/data/results` (set via `YOKO_CRAWL_RESULTS_DIR` env var)
- Guide: `docs/deployment-checklist.md`
