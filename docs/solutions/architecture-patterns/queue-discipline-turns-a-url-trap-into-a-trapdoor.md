---
title: "Queue discipline decides whether a URL trap is a tax or a trapdoor"
date: 2026-07-22
category: architecture-patterns
module: run_spider
problem_type: architecture_pattern
component: background_job
severity: high
related_components:
  - website_spider.py
  - job_manager.py
applies_when: "Building or auditing a crawler against sites with faceted search, calendars, or any combinatorial URL space"
tags:
  - crawler
  - scheduler
  - breadth-first
  - depth-first
  - url-traps
  - scrapy
  - jobdir-migration
---

# Queue discipline decides whether a URL trap is a tax or a trapdoor

> **Status:** the BFO change is in flight as PR yoko-crawler#53 (open at time of
> writing).

## Context

`run_spider.py` ran on Scrapy's defaults: `LifoMemoryQueue` (last-in-first-out —
depth-first) and `DEPTH_LIMIT = 0` (unbounded). Neither was overridden.

A real naeyc.org crawl (1,921 rows) hit a `/search/` facet subtree and never came back:

```
rows 0-429     real pages
row  430       first /search/ facet URL
rows 430-1920  facet URLs

real pages fetched after entering the facet subtree: 0
```

The whole crawl in blocks of 100 rows (`F` = facet-dominated):

```
....FFFFFFFFFFFFFFFF
```

Facet depth across those rows climbs `0 1 1 2 2 3 3 4 4 3 5 4 6 3 7...` — a DFS descent
signature, not a wide scan. Only 430 of 1,921 rows (22%) were real content.

## Guidance

**A trap's effect depends on queue discipline, not on the trap.**

- Under **breadth-first**, a trap is a **tax**: it inflates the crawl with junk, but the
  crawler keeps returning to the frontier and still reaches the real pages queued
  alongside it.
- Under **depth-first** (Scrapy's default), a trap is a **trapdoor**: the crawler
  descends and never returns, because every page inside pushes more of itself onto the
  top of the stack. A combinatorial subtree is effectively infinite, so DFS never runs
  out of trap to descend into and never unwinds to the siblings it skipped.

The conversion to breadth-first ordering:

```python
"DEPTH_PRIORITY": 1,
"SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
"SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
```

### BFO is a partial mitigation, not a bound

It bounds a **deep** trap. It does **not** bound a **shallow-wide** one — many junk URLs
fanning from one page at a single depth — because ordering by depth does nothing when
trap and real content sit at comparable depths. Measured with 400 depth-2 trap URLs
alongside 6 levels of real content, budget 300 requests:

| scheduling | real pages | trap pages |
|---|---|---|
| `DEPTH_PRIORITY=1` (BFO) | 60 | 289 |
| `DEPTH_PRIORITY=0` + Lifo (DFS) | 68 | 287 |

BFO gave **no material advantage** on this shape — it was marginally worse. WooCommerce
layered navigation is exactly this shape. A real bound needs `DEPTH_LIMIT` or a
per-path-prefix URL cap (tracked as yoko-crawler#54).

### Changing the scheduler's persistence class is a data migration

`SCHEDULER_DISK_QUEUE` controls the on-disk format of the persisted frontier, and
queuelib's Lifo and Fifo disk queues are incompatible: crossing raises
`NotADirectoryError` one way and `IsADirectoryError` the other — so a **rollback breaks
identically**.

The trap for a migration guard: `Scheduler._dqdir` mkdirs `requests.queue/` under
**both** formats, so checking that path detects nothing. The format lives one level
deeper, per priority slot — a **file** under Lifo, a **directory** under Fifo. A first
guard tested the wrong level and never fired.

Compounding it: a session failing this way exits via `shutdown`, not a crash, so the job
manager treats it as a normal close and **keeps** the jobdir — the same error repeats
forever and the domain is permanently bricked until someone clears it.

## Why This Matters

The crawl did not just waste budget on junk — it **stalled discovery**. Every request
inside the trap was a request not spent on a real page that would have surfaced more
real URLs. The two failures compound: budget lost directly, and budget lost indirectly
because the pages that would have expanded the frontier were never fetched.

After the fix, a re-crawl had discovered 3,359 URLs and fetched 907 pages the original
never reached, and was still going when measured. A trap that looks like it "only" costs
a percentage of the budget can be costing you the discovery of everything past it.

## When to Apply

- Auditing or building any stack-scheduled crawler against sites with faceted search,
  calendars, filter-pagination, or combinatorial URL space.
- Before assuming BFO "solves" URL traps — establish whether the trap is deep (BFO
  helps) or shallow-wide (it does not; use `DEPTH_LIMIT` or a per-prefix cap).
- Diagnosing a crawl that stops discovering new real content partway through. Look for
  the DFS descent signature — monotonically climbing depth into one path segment —
  rather than assuming the trap merely inflates counts.
- Before changing `SCHEDULER_MEMORY_QUEUE` / `SCHEDULER_DISK_QUEUE` on any domain with a
  persisted jobdir: treat it as a migration with a rollback path, verify at the
  per-priority-slot level, and confirm the job manager discards a jobdir that failed via
  `shutdown` rather than reusing it forever.

## Related

- yoko-crawler#52 — source issue; #53 (open PR) — the BFO fix
- yoko-crawler#54 — the shallow-wide limitation; #49 — the naeyc.org facet trap
- [Silent orphaning](../conventions/silent-orphaning-framework-extension-points.md) —
  sibling lesson from the same incident
- yoko-corpus#82 (open PR) removes the page cap that used to escalate trap sites to a
  human, which is why the shallow-wide gap matters more than it used to
