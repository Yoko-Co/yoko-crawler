# Concepts

Shared domain vocabulary for this project — entities, named processes, and status
concepts with project-specific meaning. Seeded with core domain vocabulary, then
accretes as ce-compound and ce-compound-refresh process learnings; direct edits are
fine. Glossary only, not a spec or catch-all.

## Relationships

One **Crawl** is driven as a series of **Sessions** against shared resume state. A
Session is bounded by a session cap; the Crawl continues across as many Sessions as its
own budget allows. Consumers see the Crawl; only operators see Sessions.

## Crawling

### Crawl
One logical traversal of a single site, from its **Seeds** until the frontier drains or
a budget stops it. A Crawl is the unit a consumer asks for and reasons about — it has
one identity and one result set, however many **Sessions** it took.

### Session
One execution of the crawler process within a Crawl. A Session ends either because the
frontier drained (the Crawl is done) or because it hit a session cap (the Crawl is
merely paused, and another Session resumes it). Resume state — the pending frontier and
the record of what has already been scheduled and emitted — persists between Sessions;
a Session that ends without recording why is treated as unsafe to resume, and its
resume state is discarded. Because a Session boundary is invisible to a consumer, a
paused close and a finished close must be distinguished explicitly, never inferred from
the fact that the process exited.

### Seed
The starting requests a Crawl is launched from — the site's entry URL plus the request
for its robots file, which is what leads to any sitemaps. Everything else is reached by
following links from a Seed. Because Seeds are supplied to the framework rather than
called directly, their absence degrades a Crawl silently rather than failing it, so the
count of Seeds actually emitted is treated as a health signal for the Crawl.

### Profile
A named bundle of politeness settings applied to a Crawl — request spacing,
concurrency, and the total budget. Profiles exist so that politeness is a property of
the Crawl rather than a per-operator choice; prospect sites we do not control are
crawled under the most conservative Profile.

### Trap
A region of a site that generates unbounded distinct URLs without adding content —
faceted search, calendars, filter permutations. Traps are classified by shape because
the shape determines the defence: a **deep** Trap generates volume by descending, and is
contained by visiting shallow pages first; a **shallow-wide** Trap generates volume by
fanning out at a single level, and is not — it needs an explicit bound on depth or on
how much of a Crawl any one path prefix may consume.

### Frontier
The set of URLs discovered but not yet fetched. The order in which the frontier is
drained is what decides whether a **Trap** merely costs a share of a Crawl or consumes
all of it.
