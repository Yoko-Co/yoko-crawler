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
- **Content extraction** (`content_extractor.py`) — pure, per-response helpers the spider calls to produce the additive NDJSON enrichment: trafilatura locates the main region and supplies the text to hash (counts run over the **original** lxml DOM, since trafilatura strips `<form>`/`<iframe>`); structural counts (when the main region can't be located, counts fall back to the `<body>` but with **site chrome de-chromed** — nav/aside/banner/contentinfo/search + non-article header/footer — so the theme doesn't inflate them, issue #9; `svg` is treated as non-content); surprise-embed signals; and a stable normalized SHA-256 `content_hash`. trafilatura's SIGALRM timeout is disabled (`EXTRACTION_TIMEOUT=0`) because it can't run off Scrapy's main thread
- **Embed allowlist** (`embed_allowlist.py`) — configurable benign-embed host allowlist (env `YOKO_CRAWL_BENIGN_EMBEDS`, additive) driving `embed_count_nonbenign`
- **Auth** (`auth.py`) — Bearer token via `secrets.compare_digest`

## NDJSON contract

The enrichment field names have a single source of truth: `ENRICHMENT_FIELD_NAMES` in `content_extractor.py`. `website_spider` builds its zero/empty row defaults from `content_extractor.empty_enrichment()`, and `run_spider.BASE_FEED_FIELDS` is the original five fields plus `ENRICHMENT_FIELD_NAMES` — so adding a field in one place propagates everywhere (a sync test in `tests/test_website_spider.py::TestSchemaSync` guards this). New fields are additive only — the original five (`url`, `status`, `last_modified`, `redirected_to`, `referrer`) never change. `content_text` is the one conditional column (present only under `--emit-content`). See the README "Output format" section for field semantics and the hash/normalization spec.

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
