# Yoko Crawler - Agent Instructions

## Project

Yoko Crawler — a Python/FastAPI service that runs Scrapy spiders as subprocesses to crawl websites and return discovered URLs as NDJSON. It serves as the backend for the Yoko 301s WordPress plugin's "Crawl Site" feature.

## Architecture

- **FastAPI app** (`main.py`) — API endpoints, lifespan management, single uvicorn worker (in-memory state)
- **Job Manager** (`job_manager.py`) — in-memory job dict with asyncio.Lock, max 3 concurrent crawls
- **Scrapy subprocess** — each crawl runs via `asyncio.create_subprocess_exec`, writes JSONL results and atomic status.json
- **Progress extension** (`stats_extension.py`) — Scrapy extension that writes progress to a status file every 3s. A wholesale bot-block (all-403) is NOT failed here: the crawl completes and emits its 403 rows, and the consumer (yoko-corpus) owns the blocked-crawl policy — it reads the forbidden ratio to retry with browser impersonation and, if still blocked, presents an honest "we couldn't read this site" report; failing the crawl would deny it both. Only a crawl that fetched NOTHING (every host SSRF-blocked) is failed as a genuine empty result. Bot-wall challenge pages are recognized in the spider (see below) and counted via the `waf_challenge_count` stat.
- **Bot-wall (WAF) challenge handling** (`website_spider.py`) — a Cloudflare/WAF challenge page is real HTML with a body, so without a guard the crawler would mine its markup as "content" and follow its links (e.g. Kinsta's `?ki-cf-botcl=1`), polluting the crawl with the wall's own pages. `_is_waf_challenge(response)` flags a challenge on a `403`/`429`/`503` status **plus** a Cloudflare fingerprint (`cf-mitigated` header, or `cf-ray` + `server: cloudflare`); the challenge row is still emitted (its status is the signal the corpus reads) but gets empty enrichment (not mined for content/`content_hash`) and its links are not followed. Cloudflare/Kinsta challenge query tokens (`ki-cf-botcl`, `__cf_chl_*`) are in `UNWANTED_PARAMS` so a stray challenge link isn't recorded as a distinct page.
- **Domain validator** (`domain_validator.py`) — SSRF prevention: format check + async DNS range-check at submit time, with a synchronous re-check at crawl-worker startup
- **SSRF guard** (`ssrf_guard.py`) — downloader middleware that re-resolves each request's host at fetch time and drops any resolving to a blocked/reserved range (covers both the default and curl_cffi download handlers)
- **TLS impersonation** (`tls_impersonate.py`) — Scrapy downloader middleware that tags each request with a current browser TLS fingerprint (via curl_cffi / `scrapy-impersonate`) plus a matching User-Agent, to defeat JA3/JA4 WAFs; `IMPERSONATE_CHOICES` is the single source of truth for the `--impersonate` CLI flag and the API field
- **Cookie / User-Agent injection** (`--cookies` / `--user-agent`, `CrawlRequest.cookies`/`.user_agent`) — for a site whose challenge impersonation can't beat (a JS/managed challenge), reuse a **browser-solved `cf_clearance` cookie**: the spider parses the raw Cookie-header string (`_parse_cookie_string`) and seeds it on the start requests, so Scrapy's cookie jar (`COOKIES_ENABLED` on) re-attaches it to every followed request. `user_agent` overrides the UA on all requests (via the `USER_AGENT` setting; survives impersonation since `ImpersonateMiddleware` sets UA with `setdefault`). **`cf_clearance` is bound to the UA and usually the IP that solved the challenge** — so it works only when the same UA is sent AND either the site doesn't bind the bypass cookie to IP or the crawl egresses from the solving IP. **Secret handling:** the cookie is treated as a secret — never echoed in API responses, and the job manager passes it to the subprocess via the `YOKO_CRAWL_COOKIES` **env var** (same-uid readable), NOT argv (world-readable via the process table); the `--cookies` flag stays for manual/dev use, env wins. CR/LF/NUL are rejected in the API (`CrawlRequest` validator) and stripped in `_parse_cookie_string`, so a crafted value can't inject a header.
- **Content extraction** (`content_extractor.py`) — pure, per-response helpers the spider calls to produce the additive NDJSON enrichment: trafilatura locates the main region and supplies the text to hash (counts run over the **original** lxml DOM, since trafilatura strips `<form>`/`<iframe>`); structural counts (when the main region can't be located, counts fall back to the `<body>` but with **site chrome de-chromed** — nav/aside/header/footer + banner/contentinfo/navigation/search roles — so the theme doesn't inflate them, issue #9; guarded against eating real content: chrome inside an `<article>`, or holding an `<article>`/`<main>` or substantial non-link prose, is kept; `svg` is treated as non-content); surprise-embed signals; interactive-**component** detection (sliders/carousels/accordions/tabs/galleries via container markers, `component_count`, issue #12); and a stable normalized SHA-256 `content_hash`. trafilatura's SIGALRM timeout is disabled (`EXTRACTION_TIMEOUT=0`) because it can't run off Scrapy's main thread. **`structure_hash`** (issue #36) is the content-free LAYOUT fingerprint downstream clusters into templates ("N pages across ~M templates"): the depth-limited block-tag skeleton of the content root, chrome (`nav`/`header`/`footer`) and inline tags dropped, identical sibling runs collapsed. Because lxml/libxml2 is **not** an HTML5 parser, a theme with unclosed tags in its header (an unclosed `<div>`/`<li>` in a mega-menu — common) leaves that element open and the parser nests the WHOLE page inside `<header>`, even though the source closed it; the skeleton then sees only chrome and every page of the site fingerprints as `""` → no template clusters → the discovery report reads "Not analyzed" over a complete crawl (Sarah, sais.org). So when the ordinary descent yields NO structure, `structure_hash` retries from the page's own semantic roots (`<main>` → `[role=main]` → a LONE `<article>`, `_semantic_content_roots`), found document-wide so the mis-nesting can't hide them. Four rules there are load-bearing, each one a bug that was found by review or by checking live pages: (1) the descent stays **primary**, so every page that fingerprints today keeps a byte-identical hash — verified across live well-formed sites; (2) candidates are a **list**, tried until one yields a real skeleton — an empty SPA placeholder `<main id=root></main>` beside the real `<article>` otherwise reproduces the very `""` this exists to prevent; (3) they are taken in **document order and capped** (`_MAX_ROOT_CANDIDATES`), never ranked by subtree size — ranking walked every candidate whole, so a hostile 2.4MB page of 250 nested `<main>`s (libxml2 caps nesting near 255) spent **15s** in `structure_hash` versus 0.21s, and made the choice content-length dependent so one template split as pages grew; (4) `<footer>` is the ONLY excluded ancestor (`_RESCUE_EXCLUDED_ANCESTORS`) — a footer "related story" `<article>` is page-invariant theme markup that would merge every rootless page into one bogus template, while `<header>`/`<nav>` must stay eligible because being buried under them IS the bug (on real sais.org pages the swallowed `<main>` sits under `nav.nav__main--menu` inside `header.header`; excluding `<nav>` re-broke all 525 pages, which a synthetic fixture — nesting only under `<header>` — did not reveal). There is deliberately **no** "descend into chrome" fallback either: on such a document that lands in the nav, identical on every page, and one 525-page "template" is a confident under-quote; no fingerprint beats a false merge
- **Embed allowlist** (`embed_allowlist.py`) — configurable benign-embed host allowlist (env `YOKO_CRAWL_BENIGN_EMBEDS`, additive) driving `embed_count_nonbenign`
- **Auth** (`auth.py`) — Bearer token via `secrets.compare_digest`

## NDJSON contract

The enrichment field names have a single source of truth: `ENRICHMENT_FIELD_NAMES` in `content_extractor.py`. `website_spider` builds its zero/empty row defaults from `content_extractor.empty_enrichment()`, and `run_spider.BASE_FEED_FIELDS` is the original five fields plus `ENRICHMENT_FIELD_NAMES` — so adding a field in one place propagates everywhere (a sync test in `tests/test_website_spider.py::TestSchemaSync` guards this). New fields are additive only — the original five (`url`, `status`, `last_modified`, `redirected_to`, `referrer`) never change. `content_text` is the one conditional column (present only under `--emit-content`). See the README "Output format" section for field semantics and the hash/normalization spec.

## Operator runbooks

- **Bot-blocked prospect (datacenter IP refused):** `scripts/local_scrape.sh` crawls from an
  operator's own machine and the NDJSON is hand-ingested into the corpus on the Discovery
  droplet. Full flow — Mac setup, VPN caveat, `su -s /bin/bash yoko`, `python -m cli.main
  ingest`/`analyze`, verification — is in `docs/local-scrape-runbook.md`. This is the stopgap
  until a trusted-IP proxy lets the droplet do it directly.

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
