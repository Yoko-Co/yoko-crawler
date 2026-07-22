#!/usr/bin/env bash
#
# local_scrape.sh -- crawl a bot-protected prospect FROM YOUR OWN MACHINE.
#
# Why: some Cloudflare-protected sites (e.g. urac.org) block the crawler's DigitalOcean
# datacenter IP no matter what -- a real headless browser is blocked too (see
# scripts/headless_probe.py). But the crawler's browser impersonation works fine from a
# normal residential/office IP. So the short-term fix is to run the crawl on your Mac (your
# IP) and hand the result to the corpus. The durable, team-usable fix is a trusted-IP proxy
# (tracked in a GitHub issue) -- this is the stopgap for a prospect you need NOW.
#
# One-time setup on your Mac (in this yoko-crawler directory):
#     python3 -m venv .venv && . .venv/bin/activate
#     pip install -r requirements.txt
#
# Run (turn OFF any corporate VPN first -- its exit IP is flagged like a datacenter; only a
# plain residential connection gets past these blocks):
#     . .venv/bin/activate
#     ./scripts/local_scrape.sh urac.org
#
# It writes <domain>.ndjson here, then prints the commands to run ON THE DROPLET
# (the corpus host) to turn it into a Discovery report. Use the APEX domain (urac.org, not
# www.urac.org) so it matches how Discovery normalizes the domain.
#
# Full end-to-end runbook (setup, ingest, verify, troubleshooting):
#     docs/local-scrape-runbook.md
#
set -euo pipefail

DOMAIN="${1:?usage: ./scripts/local_scrape.sh <domain>   (e.g. urac.org)}"
PROFILE="${2:-presale}"   # 'presale' = polite (serial, >=3s/page); 'standard' = faster
OUT="${DOMAIN}.ndjson"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> Crawling https://${DOMAIN}/ from THIS machine's IP with browser impersonation (${PROFILE})..."
echo ">> (a polite presale crawl of a few-hundred-page site can take 15-30 min)"

python3 "${REPO_ROOT}/run_spider.py" \
  --domain "${DOMAIN}" \
  --impersonate chrome \
  --profile "${PROFILE}" \
  --emit-content \
  --format jsonlines \
  --output "${OUT}" \
  --status-file "/tmp/${DOMAIN}-local-scrape-status.json"

LINES="$(wc -l < "${OUT}" | tr -d ' ')"
echo
echo ">> Done: ${OUT} (${LINES} page rows)."
if [ "${LINES}" -lt 3 ]; then
  echo ">> WARNING: very few rows -- the crawl looks blocked even from this machine."
  echo "   Most common cause: you're on a CORPORATE VPN. Its exit IP is flagged like a"
  echo "   datacenter -- turn the VPN OFF and re-run so the crawl uses your plain"
  echo "   residential connection (that's what gets past Cloudflare)."
  echo "   Then check ${OUT} and /tmp/${DOMAIN}-local-scrape-status.json before ingesting."
fi
echo
echo "Next, get it into the corpus so it shows up in Discovery. From your Mac:"
echo "    scp ${OUT} root@<discovery-droplet>:/tmp/"
echo "Then ON THE DROPLET (the corpus host is the Discovery droplet), as the yoko user:"
echo "    ssh root@<discovery-droplet>"
echo "    su -s /bin/bash yoko"
echo "    set -a; . /opt/yoko-corpus/yoko-corpus.env; set +a"
echo "    cd /opt/yoko-corpus/app"
echo "    /opt/yoko-corpus/venv/bin/python -m cli.main ingest ${DOMAIN} /tmp/${OUT} --profile ${PROFILE}"
echo "    /opt/yoko-corpus/venv/bin/python -m cli.main analyze ${DOMAIN}"
echo
echo "(Run as yoko, not root -- root-owned SQLite WAL files break the API/worker. Sourcing"
echo " the env file is required: without it the ingest writes to a stray empty DB.)"
echo "Full runbook: docs/local-scrape-runbook.md"
echo
echo "Open Discovery -> the ${DOMAIN} report will show the real crawl (it supersedes the"
echo "old bot-blocked one, since a readable crawl isn't treated as 'blocked')."
