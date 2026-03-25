# VPS Deployment Guide: Caddy + systemd

Deploy the crawl service directly on an Ubuntu 24.04 VPS with Caddy as the reverse proxy and systemd as the process manager.

## Prerequisites

Complete the VPS setup first:

1. **System packages**: `python3-venv`, `python3-pip`, `caddy`
2. **Directory structure**:
   - `/opt/yoko-crawl/app/` — application code
   - `/opt/yoko-crawl/venv/` — Python virtualenv
   - `/opt/yoko-crawl/results/` — crawl result files
3. **Virtualenv**: Created at `/opt/yoko-crawl/venv/` with `fastapi`, `uvicorn`, `scrapy`, `aiofiles`, `structlog` installed
4. **API key**: Generated via `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
5. **Caddy**: Configured to proxy your subdomain to `localhost:8100`
6. **systemd service**: `yoko-crawl.service` unit file with the API key in `Environment=`
7. **DNS**: A record pointing your subdomain to the VPS IP
8. **Firewall**: Ports 80 (Let's Encrypt), 443 (HTTPS), 22 (SSH) open

### Caddy config

```
# /etc/caddy/Caddyfile
crawl.example.com {
    reverse_proxy localhost:8100
}
```

Caddy automatically obtains and renews Let's Encrypt certificates.

### systemd unit

```ini
# /etc/systemd/system/yoko-crawl.service
[Unit]
Description=Yoko Crawl Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/yoko-crawl/app
Environment=YOKO_CRAWL_API_KEY=YOUR_API_KEY_HERE
ExecStart=/opt/yoko-crawl/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8100
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Deploy application code

Copy the service files to the app directory:

```bash
# From your local machine
scp main.py job_manager.py domain_validator.py auth.py \
    run_spider.py stats_extension.py website_spider.py \
    requirements.txt \
    root@YOUR_VPS:/opt/yoko-crawl/app/
```

Or clone the repo on the VPS:

```bash
cd /opt/yoko-crawl
git clone <repo-url> app
```

Install dependencies in the virtualenv (if not already done):

```bash
source /opt/yoko-crawl/venv/bin/activate
pip install -r /opt/yoko-crawl/app/requirements.txt
deactivate
```

## Start the service

```bash
systemctl daemon-reload
systemctl start yoko-crawl
systemctl status yoko-crawl
```

Check the logs:

```bash
journalctl -u yoko-crawl -f
```

## Smoke tests

Run these in order. Replace `crawl.example.com` with your subdomain and `YOUR_API_KEY` with your key.

### 1. Health check (no auth)

```bash
curl https://crawl.example.com/health
```

Expected: `{"status":"ok","active_jobs":0,"uptime_seconds":...}`

### 2. Auth enforcement

```bash
# No token — should get 401
curl -s -o /dev/null -w "%{http_code}" https://crawl.example.com/crawl/0000000000000000

# Wrong token — should get 401
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer wrong" \
  https://crawl.example.com/crawl/0000000000000000
```

### 3. SSRF protection

```bash
curl -X POST https://crawl.example.com/crawl \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain": "169.254.169.254"}'
```

Expected: `422` rejection (IP address not allowed)

### 4. Full crawl round-trip

```bash
# Start a crawl
curl -X POST https://crawl.example.com/crawl \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain": "example.com"}'
# Note the job_id from the response

# Poll status (replace JOB_ID)
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://crawl.example.com/crawl/JOB_ID
# Repeat until status is "completed"

# Stream results
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://crawl.example.com/crawl/JOB_ID/results

# Clean up
curl -X DELETE -H "Authorization: Bearer YOUR_API_KEY" \
  https://crawl.example.com/crawl/JOB_ID
```

## Updating the service

```bash
# Pull new code (or scp files again)
cd /opt/yoko-crawl/app
git pull

# Restart
systemctl restart yoko-crawl
systemctl status yoko-crawl
```

Note: Active crawls are lost on restart. They are cheap to re-run.

## Monitoring

### Service status

```bash
systemctl status yoko-crawl
journalctl -u yoko-crawl --since "1 hour ago"
```

### Caddy status

```bash
systemctl status caddy
```

### Disk usage (result files)

```bash
du -sh /opt/yoko-crawl/results/
```

Result files are automatically cleaned up 1 hour after crawl completion. If cleanup fails, files accumulate at ~55MB per crawl.

### Memory

```bash
# Check overall
free -h

# Check the uvicorn process
ps aux | grep uvicorn
```

Each Scrapy subprocess uses up to 384MB. With 3 concurrent crawls, peak usage is ~1.3GB.

## Troubleshooting

### Service won't start

```bash
journalctl -u yoko-crawl -n 50
```

Common causes:
- `YOKO_CRAWL_API_KEY` not set or too short (minimum 32 characters)
- Missing Python dependency — run `pip install -r requirements.txt` in the virtualenv
- Port 8100 already in use

### Caddy can't get TLS certificate

```bash
systemctl status caddy
journalctl -u caddy -n 50
```

Usually means DNS hasn't propagated. Verify:

```bash
dig +short crawl.example.com
```

### Crawls fail immediately

Check the per-job log files:

```bash
ls -la /opt/yoko-crawl/results/*.log
cat /opt/yoko-crawl/results/JOBID.log
```

## API key rotation

1. Generate new key: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
2. Edit the systemd unit: `systemctl edit yoko-crawl --force` and update `Environment=YOKO_CRAWL_API_KEY=NEW_KEY`
3. Restart: `systemctl restart yoko-crawl`
4. Update the API key in all WordPress instances
