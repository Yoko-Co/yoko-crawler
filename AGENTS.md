# Yoko Crawler - Agent Instructions

## Project

Yoko Crawler — a Python/FastAPI service that runs Scrapy spiders as subprocesses to crawl websites and return discovered URLs as NDJSON. It serves as the backend for the Yoko 301s WordPress plugin's "Crawl Site" feature.

## Architecture

- **FastAPI app** (`main.py`) — API endpoints, lifespan management, single uvicorn worker (in-memory state)
- **Job Manager** (`job_manager.py`) — in-memory job dict with asyncio.Lock, max 3 concurrent crawls
- **Scrapy subprocess** — each crawl runs via `asyncio.create_subprocess_exec`, writes JSONL results and atomic status.json
- **Progress extension** (`stats_extension.py`) — Scrapy extension that writes progress to a status file every 3s
- **Domain validator** (`domain_validator.py`) — three-layer SSRF prevention (format, async DNS, Scrapy DNS cache)
- **Auth** (`auth.py`) — Bearer token via `secrets.compare_digest`

## Plans

- Parent plan (full system): `docs/plans/2026-03-24-feat-site-crawler-service-plan.md`
- Python service plan: `docs/plans/2026-03-24-feat-python-crawl-service-plan.md`
