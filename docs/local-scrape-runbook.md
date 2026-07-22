# Runbook: crawling a bot-blocked prospect from your own machine

Some sites (Cloudflare Bot Management and friends) block the crawler's DigitalOcean
datacenter IP no matter what — browser TLS impersonation and a real headless browser are
both refused (see `scripts/headless_probe.py`). The same crawl from a normal
residential/office connection sails through.

So the stopgap is: **crawl on your Mac, hand the result to the corpus on the droplet.**
This runbook is the whole flow, end to end. The durable fix is a trusted-IP proxy so the
droplet can do this itself; until that lands, this is the path.

Roughly 20–40 minutes of wall clock for a few-hundred-page site, nearly all of it the
polite crawl in step 2.

---

## Step 0 — One-time setup on your Mac

In this repo:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

A **venv** ("virtual environment") is a private folder holding a Python interpreter plus
this project's libraries, so projects don't fight over package versions. `. .venv/bin/activate`
just edits your `PATH` so a plain `python` means *this* copy. You only create it once; you
re-activate it in each new terminal.

You'll also need SSH access to the Discovery droplet as `root`.

## Step 1 — Turn OFF your VPN

Not optional, and the single most common cause of a failed run. A corporate VPN's exit IP
is flagged like a datacenter IP — it gets blocked exactly like the droplet does. Only a
plain residential/office connection gets past these WAFs.

## Step 2 — Crawl, on your machine

```bash
. .venv/bin/activate
./scripts/local_scrape.sh nativeamericanagriculturefund.org
```

Use the **apex** domain (`urac.org`, not `www.urac.org`) so it matches how Discovery
normalizes domains — otherwise the report lands under a second, separate domain entry.

The script wraps `run_spider.py` with the settings this job needs: `--impersonate chrome`
(browser TLS fingerprint), `--profile presale` (serial, ≥3s between requests), and
`--emit-content` (the page text the corpus analysis needs). It writes
`<domain>.ndjson` in the repo root and a live progress file at
`/tmp/<domain>-local-scrape-status.json`.

Pass a second argument to override the profile (`./scripts/local_scrape.sh example.org standard`),
but presale is the right default for a prospect's site we don't control.

**Check the row count before going further.** The script prints it and warns under 3 rows.
A handful of rows means the crawl was blocked even from here — re-check the VPN, then look
at the `.ndjson` and the status file before ingesting anything. Ingesting a blocked crawl
publishes a wrong report.

## Step 3 — Copy it to the droplet

```bash
scp nativeamericanagriculturefund.org.ndjson root@<discovery-droplet>:/tmp/
```

The corpus lives on the **Discovery droplet** — the corpus API and the Discovery BFF must
be co-located because they share one SQLite/WAL database. See
[`yoko-corpus/deploy/README.md`](https://github.com/Yoko-Co/yoko-corpus/blob/main/deploy/README.md)
for that host's layout.

## Step 4 — Ingest and analyze, on the droplet

SSH in as `root`, then become the `yoko` service user:

```bash
ssh root@<discovery-droplet>
su -s /bin/bash yoko          # no password; root can become any user
whoami                        # -> yoko
```

`-s /bin/bash` is needed because `yoko` is a system account created with
`--shell /usr/sbin/nologin`; a plain `su yoko` would exit immediately. Its prompt won't
show a username, so confirm with `whoami`.

Then load the environment and run the two commands:

```bash
set -a; . /opt/yoko-corpus/yoko-corpus.env; set +a
cd /opt/yoko-corpus/app

/opt/yoko-corpus/venv/bin/python -m cli.main ingest \
    nativeamericanagriculturefund.org /tmp/nativeamericanagriculturefund.org.ndjson \
    --profile presale

/opt/yoko-corpus/venv/bin/python -m cli.main analyze nativeamericanagriculturefund.org

exit                          # back to root
```

Four things that each break the run if skipped:

- **Run as `yoko`, not root.** SQLite in WAL mode writes `yoko_corpus.db-wal` and
  `-shm` alongside the database. Root-owned sidecars leave the API and worker — both
  `User=yoko` — unable to write, and it fails *later*, as services that won't start or a
  UI that quietly stops updating. If you already ran it as root:
  `chown yoko:yoko /opt/yoko-corpus/data/*` (the glob matters — chowning only the `.db`
  leaves the sidecars broken).
- **Source the env file.** `config.py` defaults `YOKO_CORPUS_DB` to the *relative* path
  `yoko_corpus.db`. Without the env, the ingest silently creates an empty database in your
  current directory and Discovery never sees the data. The env file also carries the
  `YOKO_CORPUS_THRESHOLD_*` overrides that keep `analyze`'s tiers matching what the UI renders.
- **Run from `/opt/yoko-corpus/app`.** The CLI imports flat packages (`cli.*`, `db.*`,
  `services.*`) relative to the repo root.
- **There is no `yoko-corpus` command.** The corpus CLI is a Typer app invoked as
  `python -m cli.main`; the repo ships no console-script entry point.

Pass the **same** `--profile` you crawled with, so the stored crawl records how it was made.

## Step 5 — Verify

Open Discovery and reload the domain's report. A fresh readable crawl supersedes the old
bot-blocked one — the corpus treats a crawl as blocked by its forbidden-response ratio, so
a crawl that actually read the site isn't flagged.

From the CLI, `python -m cli.main report <domain>` prints the stored summary for the crawl
you just analyzed.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Crawl returns a few rows, mostly 403 | You're on a VPN, or the site needs more than TLS impersonation. Turn the VPN off and re-run. If it still fails, the site may need a browser-solved `cf_clearance` cookie — see the Cloudflare section of the [README](../README.md), and note that cookie is bound to the solving IP. |
| `Permission denied` reading the `/tmp` file as `yoko` | `scp` landed it `600`. As root: `chmod 644 /tmp/<domain>.ndjson`. |
| Ingest "succeeds" but Discovery shows nothing | You didn't source the env file — look for a stray `yoko_corpus.db` in whatever directory you ran from, delete it, and re-run properly. |
| `ModuleNotFoundError: No module named 'cli'` | You're not in `/opt/yoko-corpus/app`. |
| Services won't restart after an ingest | Root-owned WAL files. `chown yoko:yoko /opt/yoko-corpus/data/*`. |
| A domain won't re-crawl (job wedged) | Separate issue, covered under "Troubleshooting: a domain won't re-crawl" in `yoko-corpus/deploy/README.md`. |

## Housekeeping

`*.ndjson` is gitignored — these files are crawl output, often multi-megabyte, and contain
a prospect's full page text. Don't commit them, and clear out `/tmp/<domain>.ndjson` on the
droplet once the report renders.
