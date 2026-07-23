---
title: "Silent orphaning: code attached to a framework extension point stops running, and nothing reports it"
date: 2026-07-22
category: conventions
module: website_spider
problem_type: convention
component: development_workflow
severity: high
related_components:
  - requirements.txt
  - stats_extension.py
  - tests/test_website_spider.py
applies_when: "Code attaches to a framework extension point -- a decorator, hook, lifecycle callback, signal handler, or plugin registration -- rather than being called directly by your own code"
tags:
  - dependency-pinning
  - silent-failure
  - observability
  - tripwire
  - scrapy
  - extension-points
  - testing-the-real-entry-point
---

# Silent orphaning: code attached to a framework extension point stops running, and nothing reports it

> **Status:** the crawler fix is in flight as PR yoko-crawler#53 (open at time of
> writing). The pattern below is what to carry forward regardless of that PR.

## Context

`WebsiteSpider.start_requests()` seeded every crawl: the start URL, `/robots.txt`
(which fans out to XML sitemap discovery), and any injected `cf_clearance` cookie.

Scrapy 2.13 replaced `start_requests()` with `async def start()`. By 2.16 the base
method and **every call site** were gone — only two references survive in the whole
2.16 package, both in docstrings. `requirements.txt` pinned `scrapy>=2.11` with no
upper bound, so an ordinary upgrade orphaned the method.

Confirmed by instrumenting a real `Crawler` on the production version (2.16.0):

```
start_requests() called : False
seeds actually used     : ['https://example.com/']
cookies on seeds        : [{}]
robots.txt seeded       : False
```

Across six archived crawls spanning several days, **zero** contain a robots.txt row.
The only "sitemap" rows were HTML `/sitemap/` pages found by ordinary link-following.
XML sitemap discovery had not run at all, for months. The crawler was link-following
only, so sitemap-only and orphaned pages were never seen.

## Guidance

Silent orphaning needs **three conditions at once**. Breaking any one catches it —
you do not need all three fixes.

1. **A loose pin let the extension point move.** `scrapy>=2.11`, unbounded.
2. **Tests hand-called the method** (`spider.start_requests()`) instead of letting the
   framework dispatch, so they passed while production never invoked it.
3. **No runtime signal.** A crawl with no sitemap discovery is indistinguishable from
   a site that has no sitemap.

The fix breaks all three:

1. Pin extension-point frameworks: `scrapy>=2.13,<3`.
2. Share one `_seed_requests()` generator between `start_requests()` (legacy) and
   `async def start()` (current) so the two entry points cannot drift, and rewrite
   tests to drive a real `scrapy.crawler.Crawler` calling `spider.start()` — the entry
   point the framework actually uses.
3. Add a tripwire: `_seed_requests()` counts every seed it emits
   (`seeding/seeds_emitted`). A crawl that fetched pages while emitting **zero** seeds
   was seeded by something else. Surfaced in the status file next to `robots_fetched`
   and `sitemaps_fetched`, and logged as a loud ERROR.

Deliberately does **not** fail the crawl: link-following-only is degraded, not
worthless, and failing would discard real pages over a defect an operator can fix and
re-run.

**The generalisable pattern:** count the thing that should have happened, and assert
loudly when the count is zero but the surrounding work succeeded. Applies to any hook,
callback, signal handler, or plugin registration.

## Why This Matters

Every ordinary layer of defence reported success:

- The dependency upgrade succeeded — the pin was satisfiable.
- The test suite was green — it called the orphaned method directly.
- The application ran and produced output — crawls completed, pages were archived.
- Nothing in logs, CI, or monitoring distinguished "ran the full pipeline" from "ran a
  degraded subset."

The failure mode is not a crash, an exception, or a red test. It is the **complete
absence of a symptom**. It was found by auditing production behaviour against
expectations, not by anything breaking.

That is the tell: if a bug can only be found by an audit rather than by something
failing, the system is missing an observability primitive, not a test case.

## When to Apply

Only frameworks with **extension points** carry this risk. A plain library that renames
a function produces a loud `ImportError` or `TypeError` at call time — self-defending.
So do not treat every unbounded pin as equal risk: `fastapi`, `typer`, `scrapy`,
`django`, WordPress hook systems matter; `structlog`, `aiofiles`, `requests` much less.

Treat it as live risk when **all** of these hold:

- The dependency is pinned `>=X` with no `<Y`, **and** exposes extension points.
- Tests invoke the handler directly rather than through framework dispatch.
- There is no count or metric for "how many times did this hook fire", so a silent stop
  looks like normal reduced activity.

When auditing, for every hook registration ask: (a) is the pin bounded, (b) do tests
exercise real dispatch, (c) is there a nonzero-activity assertion. Any "no" is a risk.

## Examples

**yoko-crawler** — `scrapy>=2.11` unbounded → `start_requests()` orphaned by 2.13/2.16
→ robots.txt and sitemap discovery silently stopped for months, with zero failing tests
and zero errors logged. Fix in flight as PR #53 (issues #52, #55).

**yoko-301s** — found by auditing for this same pattern. 49 abilities register on
`wp_abilities_api_init`, a WordPress 6.9+ hook, while the plugin header declares
`Requires at least: 6.8`. On 6.8 — a supported version — the hook never fires, all 49
registrations are skipped, and nothing reports it. No `function_exists()` guard, no
signal. Its own comment concedes it:

```php
 * Gracefully degrades on older versions — the wp_abilities_api_init hook simply never fires.
```

Same three conditions: a compatibility gap that lets the extension point be absent, no
test running registration against a 6.8-shaped environment, and no count of
successfully-registered abilities to compare against the expected 49. Filed as
yoko-301s#95.

## Related

- yoko-crawler#52 — the incident; yoko-crawler#55 — the audit
- yoko-crawler#53 (open PR) — pin cap, entry-point tests, tripwire
- [Queue discipline makes a trap a trapdoor](../architecture-patterns/queue-discipline-turns-a-url-trap-into-a-trapdoor.md) —
  sibling lesson from the same incident, different failure class
- `README.md` claims "Seeds from robots.txt sitemaps, then follows all internal links".
  That is the exact claim this incident silently falsified; the `seeds_emitted`
  tripwire is what finally backs it with a runtime guarantee.
