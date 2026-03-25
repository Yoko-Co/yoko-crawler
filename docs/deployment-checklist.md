# Deployment Checklist: Python Crawl Service (FastAPI + Scrapy)

**Target:** Docker container (2GB RAM host)
**Domain:** crawl.example.com (replace with your domain)


## 1. Docker Build

### Dockerfile Correctness

- [ ] Base image is `python:3.13-slim-bookworm`
- [ ] System dependencies installed in a single `RUN` layer with `rm -rf /var/lib/apt/lists/*` cleanup
- [ ] `requirements.txt` is copied and installed BEFORE `COPY . .` (layer caching)
- [ ] Non-root user `crawluser` created and switched to via `USER crawluser`
- [ ] `/data/results` directory created and owned by `crawluser` before `USER` switch
- [ ] `EXPOSE 8000` matches the uvicorn `--port 8000` argument
- [ ] `--no-cache-dir` flag on `pip install`
- [ ] `--timeout-graceful-shutdown 15` on uvicorn CMD
- [ ] `.dockerignore` prevents `.env`, `.git`, `docs/`, tests from entering the image

### Image Size Verification

Run after build:
```bash
docker images yoko-crawler --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
```
**Expected:** Under 500MB.

### Layer Cache Test

After a code-only change (edit `main.py`), rebuild and confirm:
```bash
docker compose build 2>&1 | grep -c "CACHED"
```
**Expected:** The `pip install` layer should be CACHED.


## 2. Docker Compose

### Memory Limits

- [ ] `deploy.resources.limits.memory: 1536M` is set
- [ ] Scrapy's `MEMUSAGE_LIMIT_MB=384` in `run_spider.py` (3 × 384 = 1152MB, fits within container)
- [ ] Remaining ~384MB for FastAPI + uvicorn + OS overhead
- [ ] No swap configured

**Verify after deploy:**
```bash
docker stats --no-stream crawl-service --format "{{.MemUsage}} / {{.MemLimit}}"
```
**Expected:** Something like `120MiB / 1.5GiB` at idle.

### Security Hardening

- [ ] `security_opt: no-new-privileges:true`
- [ ] `cap_drop: ALL`
- [ ] `read_only: true`
- [ ] Named volume at `/data/results` (separate from tmpfs at `/tmp`)

### Healthcheck

- [ ] Uses `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"` (no curl in slim image)
- [ ] `interval: 30s`, `timeout: 10s`, `retries: 3`, `start_period: 10s`

**Verify after deploy:**
```bash
docker inspect crawl-service --format "{{.State.Health.Status}}"
```
**Expected:** `healthy`

### Log Rotation

- [ ] `logging.driver: json-file` with `max-size: 10m` and `max-file: 3`
- [ ] Maximum disk usage from container logs: 30MB

### Restart Policy

- [ ] `restart: unless-stopped`
- [ ] `stop_grace_period: 30s` — exceeds uvicorn's 15s graceful shutdown


## 3. nginx

### TLS Certificate Setup

- [ ] DNS A record for `crawl.example.com` resolves to droplet IP BEFORE running certbot
- [ ] Certbot installed: `apt install certbot python3-certbot-nginx`
- [ ] Certificate obtained: `certbot certonly --nginx -d crawl.example.com --deploy-hook "systemctl reload nginx"`
- [ ] Certificate files exist at `/etc/letsencrypt/live/crawl.example.com/`

### TLS Hardening (already in nginx config)

- [ ] `ssl_protocols TLSv1.2 TLSv1.3`
- [ ] `ssl_ciphers` with ECDHE+AES-GCM only
- [ ] `ssl_session_tickets off`
- [ ] OCSP stapling enabled

### Security Headers (already in nginx config)

- [ ] `Strict-Transport-Security` with 2-year max-age
- [ ] `X-Content-Type-Options: nosniff`
- [ ] `X-Frame-Options: DENY`
- [ ] `Referrer-Policy: no-referrer`
- [ ] `server_tokens off`

### Rate Limiting (already in nginx config)

- [ ] `POST /crawl`: 1r/m per IP with burst=3
- [ ] General API: 10r/s per IP with burst=20

### Proxy Settings

- [ ] `proxy_read_timeout 300s` — exceeds WordPress plugin's 120s timeout
- [ ] `proxy_buffering off` — required for StreamingResponse
- [ ] `client_max_body_size 1k`

### Config Installation

- [ ] Copy `nginx/crawl.example.com.conf` to `/etc/nginx/sites-available/`
- [ ] Symlink: `ln -s /etc/nginx/sites-available/crawl.example.com /etc/nginx/sites-enabled/`
- [ ] Test: `nginx -t`
- [ ] Reload: `systemctl reload nginx`


## 4. Secrets

### API Key Generation

- [ ] Generate key on droplet: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- [ ] Key is 64 characters (meets 32-char minimum enforced by lifespan)

### .env File

- [ ] `.env` created in project directory: `YOKO_CRAWL_API_KEY=<key>`
- [ ] Permissions restricted: `chmod 600 .env`
- [ ] `.env` is in both `.gitignore` and `.dockerignore`

### Verify Auth Works

```bash
# Health (no auth required)
curl -s -o /dev/null -w "%{http_code}" https://crawl.example.com/health
# Expected: 200

# Missing token
curl -s -o /dev/null -w "%{http_code}" https://crawl.example.com/crawl/0000000000000000
# Expected: 401

# Wrong token
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer wrong" https://crawl.example.com/crawl/0000000000000000
# Expected: 401
```

### Rotation Procedure

1. Generate new key: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
2. Edit `.env` on droplet
3. Restart: `docker compose up -d`
4. Update API key in all WordPress instances
5. Verify from each instance


## 5. Monitoring

### Required

- [ ] External uptime monitor (UptimeRobot, Hetrix, etc.) on `https://crawl.example.com/health`
- [ ] Digital Ocean monitoring agent enabled
- [ ] DO alerts: CPU > 90% (10min), Memory > 90% (5min), Disk > 80%

### Recommended

- [ ] Docker image prune cron: `0 3 * * 0 docker image prune -f --filter "until=168h"`
- [ ] Periodic OOM check: `docker logs crawl-service 2>&1 | grep -i "killed\|oom\|memory"`


## 6. Firewall

```bash
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (certbot + redirect)
ufw allow 443/tcp   # HTTPS
ufw deny 8100/tcp   # Explicit deny (belt + suspenders)
ufw enable
```

Verify port 8100 is blocked externally:
```bash
curl --connect-timeout 5 http://<droplet-ip>:8100/health
# Expected: Connection refused or timeout
```


## 7. Certbot Auto-Renewal

- [ ] Timer exists: `systemctl list-timers | grep certbot`
- [ ] Dry run passes: `certbot renew --dry-run`
- [ ] Deploy hook configured: `--deploy-hook "systemctl reload nginx"`
- [ ] Expiry email goes to a monitored address


---


## Ordered Deployment Runbook

### Phase 1: Droplet Preparation

- [ ] Provision DO droplet (2GB RAM, Ubuntu 22.04+)
- [ ] SSH in: `apt update`
- [ ] Install Docker + Docker Compose
- [ ] Install nginx: `apt install nginx`
- [ ] Install certbot: `apt install certbot python3-certbot-nginx`
- [ ] Configure firewall (see Section 6)
- [ ] Enable DO monitoring agent

### Phase 2: DNS and TLS

- [ ] Create A record: `crawl.example.com` -> droplet IP (TTL 300)
- [ ] Wait for propagation: `dig @8.8.8.8 crawl.example.com +short`
- [ ] Obtain cert: `certbot certonly --nginx -d crawl.example.com --deploy-hook "systemctl reload nginx"`
- [ ] Verify: `certbot renew --dry-run`

### Phase 3: nginx

- [ ] Copy config, symlink, `nginx -t`, `systemctl reload nginx`

### Phase 4: Application

- [ ] Clone project to droplet
- [ ] Generate API key, create `.env`, `chmod 600 .env`
- [ ] Build and start: `docker compose up -d --build`
- [ ] Wait 15 seconds for startup

### Phase 5: Post-Deploy Verification

- [ ] `docker compose ps` shows `Up (healthy)`
- [ ] `curl https://crawl.example.com/health` returns `{"status":"ok",...}`
- [ ] Auth enforced (401 without token)
- [ ] Memory limit enforced: `docker stats --no-stream`
- [ ] Port 8100 blocked externally
- [ ] HTTP redirects to HTTPS (301)
- [ ] Logs clean: `docker compose logs --tail 20`

### Phase 6: Smoke Test

```bash
# Start crawl
curl -X POST https://crawl.example.com/crawl \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"domain": "example.com"}'
# Expected: 202 with job_id

# Poll status (use job_id from above)
curl -H "Authorization: Bearer <key>" https://crawl.example.com/crawl/<job_id>
# Expected: queued -> running -> completed

# Fetch results
curl -H "Authorization: Bearer <key>" https://crawl.example.com/crawl/<job_id>/results
# Expected: NDJSON stream

# Delete
curl -X DELETE -H "Authorization: Bearer <key>" https://crawl.example.com/crawl/<job_id>
# Expected: {"deleted": true}

# SSRF protection
curl -X POST https://crawl.example.com/crawl \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"domain": "169.254.169.254"}'
# Expected: 422 rejection
```

### Phase 7: WordPress Integration

- [ ] Configure crawl service URL in plugin: `https://crawl.example.com`
- [ ] Configure API key in plugin settings
- [ ] Trigger crawl from WordPress admin
- [ ] Verify crawl completes and URLs are imported

### Phase 8: Monitoring Setup

- [ ] External uptime monitor configured
- [ ] DO alerts set
- [ ] Docker image prune cron added
- [ ] Certbot renewal verified

### Phase 9: First 24 Hours

- [ ] +1 hour: `docker compose ps`, `docker stats --no-stream`, `df -h /`
- [ ] +4 hours: `docker compose logs --tail 100` — check for errors
- [ ] +24 hours: `du -sh /var/lib/docker/volumes/yoko-crawler_crawl-results/_data/` — verify cleanup working
- [ ] Increase DNS TTL to 3600


---

## Rollback

**This is a stateless service with no database.** Rollback is straightforward:

1. `docker compose down`
2. `git checkout <good-commit>`
3. `docker compose up -d --build`

In-flight crawls are lost on restart (by design — crawls are cheap to restart).
