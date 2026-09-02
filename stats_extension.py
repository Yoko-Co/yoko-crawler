"""
Scrapy extension that writes crawl progress to an atomic status file.

Written every 3 seconds during the crawl and once on spider close.
The FastAPI parent process reads this file to serve GET /crawl/{id}.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from scrapy import signals
from twisted.internet.task import LoopingCall

logger = logging.getLogger(__name__)


def _fail_on_seeding_incomplete() -> bool:
    """Whether a seeding-incomplete crawl is FAILED or merely logged (issue #102).

    A kill switch, defaulting to on, and the honest reason for it is that this converts a log
    line that has existed since #76 into a client-visible failure with NO production evidence
    of how often the condition fires -- that log has only ever gone to the Scrapy log of a
    hand-managed droplet, where nothing reads it. If the answer turns out to be "more often
    than expected", rollback is `YOKO_CRAWL_FAIL_SEEDING_INCOMPLETE=0` plus a restart, rather
    than redeploying a hand-managed venv.

    The LOG is unconditional either way, so turning this off still leaves the evidence."""
    return os.environ.get("YOKO_CRAWL_FAIL_SEEDING_INCOMPLETE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


class ProgressWriter:
    """Scrapy extension that writes progress to an atomic JSON status file."""

    # Safety-valve close reasons that produce valid (possibly partial) results.
    _COMPLETED_REASONS = {"finished", "closespider_timeout", "closespider_itemcount"}

    def __init__(self, stats, status_file):
        self.stats = stats
        self.status_file = status_file
        self._loop = None

    @classmethod
    def from_crawler(cls, crawler):
        status_file = crawler.settings.get("STATUS_FILE")
        if not status_file:
            raise ValueError(
                "STATUS_FILE setting is required for ProgressWriter extension"
            )
        ext = cls(crawler.stats, status_file)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider):
        self._loop = LoopingCall(self._write_status, "running")
        self._loop.start(3.0)

    def spider_closed(self, spider, reason):
        if self._loop and self._loop.running:
            self._loop.stop()
        status = "completed" if reason in self._COMPLETED_REASONS else "failed"
        error = reason if status == "failed" else None
        # Structured failure discriminator (issue #44): a stable token a consumer
        # (yoko-corpus) switches on instead of scraping `error`/`close_reason` prose.
        # None on a real crawl; set alongside every failed branch below. An abnormal
        # Scrapy close (memusage/OOM/signal) is a generic `crawl_error`; the empty
        # guards below refine it to the specific cause.
        failure_reason = "crawl_error" if status == "failed" else None

        # NOTE: an all-403/blocked crawl (bot-wall) is intentionally NOT failed here. The
        # crawl COMPLETES and emits its 403 rows; the consumer (yoko-corpus) owns the
        # blocked-crawl policy -- it reads the forbidden ratio to retry with browser
        # impersonation and, if still blocked, presents an honest "we couldn't read this
        # site" report. Failing the crawl here would deny the corpus both. The spider's
        # `waf_challenge_count` stat records the wall for observability. (The empty-crawl
        # guards below still fail a crawl that fetched NOTHING -- a genuine empty result.)

        # A crawl that fetched NOTHING gets classified by WHY, so the consumer can tell a
        # mistyped/unreachable target from an SSRF-blocked one instead of a misleading
        # "completed" with zero results. Only ever reclassifies an empty crawl: any
        # crawl that fetched even one page (incl. an all-403 bot-wall) is left completed.
        if status == "completed":
            responses = self.stats.get_value("response_received_count", 0)
            if responses == 0:
                blocked = self.stats.get_value("ssrf_guard/blocked", 0)
                # A scheme refusal is also an SSRF-guard drop (#89). Without counting it
                # here, a crawl the guard emptied by refusing `file://`/`s3://` fell through
                # to the generic `unreachable`, whose operator-facing text says the address
                # is wrong and to check it -- sending someone to re-type a domain that was
                # never the problem.
                blocked += self.stats.get_value("ssrf_guard/blocked_scheme", 0)
                exceptions = self.stats.get_value("downloader/exception_count", 0)
                # Order matters: the SSRF guard drops a host via IgnoreRequest, which Scrapy
                # ALSO counts in downloader/exception_count -- so an all-SSRF-blocked crawl
                # has exceptions>0 too. Checking blocked>0 first keeps it `ssrf_blocked`
                # (the specific cause) rather than the generic `unreachable`.
                if blocked > 0:
                    # Every candidate host resolved to a blocked/reserved range and was
                    # dropped by the SSRF guard.
                    status, failure_reason = "failed", "ssrf_blocked"
                    error = (
                        "crawl blocked by SSRF guard: every target host resolved to "
                        "a private or reserved address; no pages were fetched"
                    )
                elif exceptions > 0:
                    # Every request errored at the transport layer (DNS / connection /
                    # TLS) and nothing was fetched -> the target is unreachable, almost
                    # always a wrong or mistyped address.
                    status, failure_reason = "failed", "unreachable"
                    error = (
                        "target unreachable: every request failed at the network layer "
                        "(DNS or connection) and no pages were fetched -- check the address"
                    )
                # else: 0 responses, no SSRF drops, no transport errors -> a genuinely
                # empty finish (e.g. everything robots-disallowed). Left "completed" here --
                # though since #102 the phase-two tripwire below can still reclassify it when
                # robots.txt responded and the start URL was never emitted.

        # Seeding tripwire (issue #52). Our own seeding method counts every seed it emits,
        # so a crawl that fetched pages while reporting ZERO seeds was seeded by something
        # else -- in practice a framework rename orphaning the method, exactly what
        # silently disabled robots.txt/sitemap discovery for months. Log it loudly: this
        # class of failure produces a plausible-looking crawl (pages ARE fetched, just
        # fewer), so nothing else would ever surface it. Deliberately NOT failed -- a
        # link-followed crawl is degraded, not worthless, and failing it would lose real
        # pages over a defect the operator can fix and re-run.
        if self.stats.get_value("seeding/seeds_emitted", 0) == 0 and \
                self.stats.get_value("response_received_count", 0) > 0:
            logger.error(
                "SEEDING DID NOT RUN: the crawl fetched pages but emitted no seeds of its "
                "own, so robots.txt and sitemap discovery were skipped and this crawl is "
                "link-following only. Almost certainly the Scrapy seeding entry point was "
                "renamed and WebsiteSpider.start() is no longer being called -- check the "
                "installed Scrapy against the pin in requirements.txt."
            )

        # Phase-two seeding tripwire (issue #76), and unlike its sibling above this one can
        # FAIL the crawl (#102). The asymmetry is deliberate: the #52 case leaves a crawl that
        # fetched REAL PAGES, just fewer, and failing it would discard genuine work over a
        # defect an operator can fix and re-run. This case leaves no page of the site at all --
        # nothing to preserve, and calling it `completed` hands the corpus an "inventory" of
        # robots.txt. Seeding is no longer one atomic event:
        # the spider seeds robots.txt, and the START URLS are emitted from its callback once
        # the Disallow rules are final. So "seeding ran" is no longer proved by a non-zero
        # seeds_emitted -- phase one can succeed while phase two never happens (an unbounded
        # robots.txt redirect chain, an exception in the callback, a request lost to a
        # middleware that neither calls back nor errbacks). The signature is robots fetched,
        # start URLs never emitted, and the crawl closing "completed" with a one-row
        # inventory -- the most deceptive shape available, so it gets its own loud error.
        if self.stats.get_value("seeding/seeds_emitted", 0) > 0 and \
                self.stats.get_value("seeding/start_urls_emitted", 0) == 0:
            logger.error(
                "SEEDING STOPPED AFTER ROBOTS.TXT: the crawl fetched robots.txt but never "
                "emitted its start URL, so it inventoried nothing. Any page count in this "
                "crawl is robots.txt itself, NOT the site -- do not read it as an inventory. "
                "Usually an unterminated robots.txt redirect chain or an error in "
                "parse_robots; re-run and check the robots.txt of the target."
            )
            # ...and FAIL it, when -- and only when -- this crawl inventoried NOTHING (#102).
            #
            # The condition above is a LOG predicate, and has been since #76. It was never
            # sized to carry a status flip, which is the mistake review caught: driving the
            # real ProgressWriter, a session reporting FOUR THOUSAND pages came back
            # `failed / seeding_incomplete`, writing `urls_crawled: 4000` beside an error
            # reading "no page of the site was fetched".
            #
            # The route is the production one. yoko-corpus runs one logical crawl as N
            # sessions against a JOBDIR, so session 2+ restores a full frontier and fetches
            # from it. If phase two does not run in THAT session, the counter is 0 while the
            # session crawls the site perfectly well. And the `completed` gate does not help:
            # `closespider_timeout`/`closespider_itemcount` are COMPLETED reasons, and they
            # are how every session of that path ends.
            #
            # So the flip has to prove its own claim. It fires only when every response this
            # session received was seeding traffic -- robots.txt, sitemaps, sitemap probes --
            # meaning no page of the site was reached. Deliberately UNDER-counts seeding
            # responses if a category is ever added and not listed here: the guard then goes
            # SILENT rather than wrong, which is the correct direction for a check that turns
            # a client's crawl red.
            #
            # The log keeps the wider condition on purpose. The two answer different
            # questions: the log says "phase two did not run" (diagnostic, always worth
            # knowing), the flip says "and therefore this crawl inventoried nothing"
            # (a verdict, which needs the stronger evidence).
            seeding_responses = sum(self.stats.get_value(k, 0) for k in (
                "seeding/robots_fetched",
                "seeding/sitemaps_fetched",
                "seeding/sitemap_probes_found",
                "seeding/sitemap_probes_missed",
                "seeding/sitemap_probes_not_a_sitemap",
            ))
            responses = self.stats.get_value("response_received_count", 0)
            # Keyed on robots.txt having RESPONDED, not on the seed having been emitted
            # (`seeds_emitted` is bumped before the request is even scheduled), so the error
            # text below is provably true when it is written.
            robots_responded = self.stats.get_value("seeding/robots_fetched", 0) > 0
            # Gated on `completed` so an abnormal close keeps `crawl_error`, which names a
            # cause where this names only a symptom. NOT, as an earlier draft claimed,
            # because an SSRF-blocked or unreachable crawl "reaches this same condition": it
            # does not. An IgnoreRequest or a transport failure on the robots seed routes to
            # the `robots_failed` errback, whose last act is `_start_url_requests()`, so those
            # crawls always have `start_urls_emitted == 1` and never arrive here. The
            # precedence tests stay as ordering pins, but they stage a shape production does
            # not produce.
            #
            # It does NOT fire for a robots-restricted site either -- the false positive worth
            # naming, since that was the design question. `_start_url_requests` is deliberately
            # not routed through `_schedule`, so a `Disallow: /` site still emits and counts
            # its start URL, and that start-URL response is not seeding traffic.
            #
            # (An earlier draft called gastro.org "a one-page crawl". It is not: robots.txt is
            # unconditionally emitted as its own row, so the floor for any crawl that reads
            # robots.txt is TWO rows -- the AGENTS.md line this borrowed from has the same
            # undercount. Corrected because the number is load-bearing: a guard keyed on "one
            # row" would have been wrong.)
            if (status == "completed" and robots_responded
                    and responses <= seeding_responses
                    and _fail_on_seeding_incomplete()):
                status, failure_reason = "failed", "seeding_incomplete"
                error = (
                    "seeding stopped after robots.txt: the start URL was never emitted, so "
                    "no page of the site was fetched. Any row in this crawl is robots.txt "
                    "or a sitemap -- it is not an inventory"
                )

        self._write_status(
            status, error=error, final=True, close_reason=reason, failure_reason=failure_reason
        )

    def _robots_root_disallowed(self):
        """True/False once robots.txt has been parsed, None when it never was (no
        robots.txt, a transport failure, or a crawl that ended first). None matters: it
        keeps "we could not read the rules" distinct from "the rules allow us"."""
        raw = self.stats.get_value("robots_root_disallowed", None)
        return None if raw is None else bool(raw)

    def _robots_readability(self):
        """`{outcome, final_status, waf_wall}` for the robots.txt fetch (issue #97).

        `outcome` is one of:
          `parsed`     -- we hold rules for this crawl (this session or restored from state)
          `absent`     -- 404/410, the one status class that genuinely means "no rules"
          `unreadable` -- a transport failure, a non-200, or a body we could not parse: rules
                          may exist and we did not see them. THIS is the allow-all we cannot
                          justify, and the count #97 needs to settle its posture question.
          `unknown`    -- robots.txt never resolved (the crawl ended first)

        `waf_wall` separates "a WAF would not let us ask" from "the origin said no" via the
        spider's existing `_is_waf_challenge`; only meaningful when outcome is `unreadable`."""
        outcome = self.stats.get_value("robots_readability_outcome", None)
        status = self.stats.get_value("robots_readability_status", None)
        if not outcome:
            # Tripwire, same reasoning as the two seeding tripwires above (#52/#76): the
            # recorder runs on every route out of `parse_robots`, so start URLs emitted with
            # no outcome recorded means the recorder itself stopped being reached. Reported
            # as its own value rather than `unknown`, because `unknown` legitimately means
            # "the crawl ended before robots resolved" and a broken instrument must not hide
            # inside a legitimate outcome -- that is how #52 went unnoticed for months.
            emitted = self.stats.get_value("seeding/start_urls_emitted", 0)
            if emitted:
                logger.error(
                    "ROBOTS READABILITY NOT RECORDED: the crawl emitted start URLs but "
                    "logged no robots.txt readability outcome, so `_record_robots_readability` "
                    "was not reached on any route out of parse_robots. The allow-all signal "
                    "for this crawl is missing, not clean -- do not read it as `parsed`. "
                    "(issue #97)"
                )
            outcome = "not_recorded" if emitted else "unknown"
        return {
            "outcome": outcome,
            # None on a transport failure -- there was no response to have a status.
            "final_status": None if status is None else int(status),
            # Cloudflare-specific by construction; see `_record_robots_readability`.
            "cf_wall": bool(self.stats.get_value("robots_readability_cf_wall", False)),
            # We hold rules from an earlier session even though THIS fetch was refused.
            "rules_from_state": bool(
                self.stats.get_value("robots_readability_rules_from_state", False)),
        }

    def _status_counts(self):
        """HTTP status histogram ({"200": n, "403": n, ...}) from Scrapy's built-in
        `downloader/response_status_count/<code>` stats, so the operator can see the response
        mix (how many 403s, redirects, 404s) at a glance without re-deriving it."""
        prefix = "downloader/response_status_count/"
        counts = {}
        for key, value in self.stats.get_stats().items():
            if key.startswith(prefix):
                counts[key[len(prefix):]] = value
        return counts

    def _write_status(self, status, error=None, final=False, close_reason=None, failure_reason=None):
        data = {
            "status": status,
            "urls_discovered": self.stats.get_value("scheduler/enqueued", 0),
            "urls_crawled": self.stats.get_value("response_received_count", 0),
            "updated_at": time.time(),
            "error": error,
            # The Scrapy close reason, surfaced even on a "completed" close so a
            # consumer can tell a natural `finished` from a safety-valve stop
            # (`closespider_timeout` / `closespider_itemcount`) that produced only
            # partial results. None while the crawl is still running.
            "close_reason": close_reason,
            # Structured failure token (issue #44): None unless the crawl failed with a
            # classified cause (unreachable / ssrf_blocked / crawl_error /
        # spider_init_error / seeding_incomplete).
            "failure_reason": failure_reason,
            # Seeding observability (issue #52). `seeds_emitted` is the tripwire for a whole
            # bug CLASS: a framework rename made the spider's seeding method unreachable and
            # Scrapy's default seeded instead, so robots.txt/sitemap discovery silently
            # stopped -- for months, with no exception, no failing test and no log line.
            # A crawl seeded by anything other than our own method reports 0 here.
            # `robots_fetched` separates "the site lists no sitemap" (robots fetched,
            # sitemaps 0) from "we never asked" (robots 0) -- and since #76 a third case,
            # "we asked and the transport failed", which `robots_failed` distinguishes.
            "seeding": {
                "seeds_emitted": self.stats.get_value("seeding/seeds_emitted", 0),
                "robots_fetched": self.stats.get_value("seeding/robots_fetched", 0),
                "sitemaps_fetched": self.stats.get_value("seeding/sitemaps_fetched", 0),
                # Conventional-location sitemap probes (issue #77), sent only when
                # robots.txt named no sitemap. `found` > 0 means the site HAS a sitemap it
                # never told us about -- coverage we would previously have lost in silence.
                "sitemap_probes_sent": self.stats.get_value("seeding/sitemap_probes_sent", 0),
                "sitemap_probes_found": self.stats.get_value("seeding/sitemap_probes_found", 0),
                # Non-zero means robots.txt DISALLOWED the conventional locations, so we
                # never looked. Without this, `sitemap_probes_sent: 0` conflates "robots
                # named a sitemap, nothing lost" with "we were refused, coverage lost" --
                # exactly the ambiguous zero `robots_failed` exists to prevent.
                "sitemap_probes_disallowed": self.stats.get_value(
                    "seeding/sitemap_probes_disallowed", 0
                ),
                # Seeding is two-phase since #76 (robots.txt, THEN the start URLs from its
                # callback). A crawl with seeds_emitted > 0 but start_urls_emitted == 0
                # fetched robots.txt and nothing else -- see the tripwire below.
                "start_urls_emitted": self.stats.get_value("seeding/start_urls_emitted", 0),
                # Non-zero means robots.txt could not be FETCHED (DNS/refused/timeout) and
                # the crawl ran allow-all. Without this, `robots_fetched: 0` is ambiguous
                # between "the seeder never ran" and "we asked and the network refused" --
                # and the crawl's robots posture is unrecoverable after the fact.
                "robots_failed": self.stats.get_value("seeding/robots_failed", 0),
                # 1 means the installed Scrapy does NOT honour the `depth_reset` meta key
                # (added in 2.18), so the start URL sat at depth 1 and every page below it
                # is off by one (#81). Inert while DEPTH_LIMIT is unset, but it must not be
                # invisible: the whole point of #81 is that an unsupported key is ignored in
                # SILENCE, and a droplet venv is hand-managed, so the requirements floor does
                # not bind it. Reported even when 0, so the field is a positive assertion the
                # mechanism works rather than an ambiguous absence.
                "depth_reset_unsupported": self.stats.get_value(
                    "seeding/depth_reset_unsupported", 0
                ),
            },
            # Block/restriction observability. These are OBSERVED counts, NOT a verdict: on a
            # Cloudflare-fronted site a single 403 can be both a bot challenge and a login page
            # (INCOSE's /setdb-login/ returns `403 cf-mitigated: challenge` to a bot), and the
            # crawler can't recover the origin's intent from one blocked fetch. So it surfaces
            # the raw picture and leaves the blocked-crawl policy to the consumer (yoko-corpus
            # already reads the forbidden ratio; the frontend can show it).
            #   waf_challenge_count    -- responses Cloudflare ITSELF walled (cf-mitigated, or a
            #                             CF fingerprint with no origin headers).
            #   origin_forbidden_count -- 403s the ORIGIN generated (member-restricted content
            #                             we DO want inventoried), kept out of the wall bucket.
            #   status_counts          -- full HTTP status histogram, from Scrapy's own stats.
            "blocking": {
                "waf_challenge_count": self.stats.get_value("waf_challenge_count", 0),
                "origin_forbidden_count": self.stats.get_value("origin_forbidden_count", 0),
                "status_counts": self._status_counts(),
            },
            # Platform fingerprints observed on the first HTML response (corpus #112).
            # OBSERVATION, not a verdict: the crawler records the headers and head tags it
            # saw; yoko-corpus maps them onto a platform name. Empty when the site never
            # identified itself, which a consumer must read as "no signal" -- NOT as "not
            # WordPress". Detection used to run on the URL space alone, which is structurally
            # blind to a WordPress site with pretty permalinks (`wp-json`/`wp-admin` are
            # paths we are built never to schedule, and `/wp-content/` only surfaces as a
            # linked-file asset row), so the better configured the site the less likely we
            # identified it.
            "platform_signals": self.stats.get_value("platform_signals", {}) or {},
            # Transport-failure coverage (issue #73): URLs that produced NO response at all
            # -- DNS failure, refused connection, TLS error, per-request timeout. Distinct
            # from a 4xx/5xx (which `blocking.status_counts` already carries) and from the
            # auth-gated skips (#43). Counted after retries are exhausted, and never
            # counting our own SSRF-guard refusals or the cancellations a crawl close
            # produces -- neither is the site failing to answer.
            "unreachable": {
                "total": self.stats.get_value("transport_failures", 0),
                "dns": self.stats.get_value("transport_failures/dns", 0),
                "connection": self.stats.get_value("transport_failures/connection", 0),
                "timeout": self.stats.get_value("transport_failures/timeout", 0),
                "tls": self.stats.get_value("transport_failures/tls", 0),
                "other": self.stats.get_value("transport_failures/other", 0),
            },
            # Restriction observability (issue #74). The crawler deliberately does not fetch
            # several classes of URL, and until now NONE of those counts left Scrapy's stats
            # -- so a crawl the site itself had walled off was indistinguishable from a crawl
            # of a small site. gastro.org made that concrete: `User-agent: * / Disallow: /`
            # reduced a 2,347-URL site to a 1-page inventory that still reported "completed",
            # and the report called it "a Simple site".
            #
            # Like `blocking`, these are OBSERVED counts, not a verdict -- the consumer
            # decides what they mean. `robots_disallowed` is the load-bearing one: large
            # relative to the pages actually crawled, the crawl is not an inventory.
            "restrictions": {
                # Deterministic, discovery-independent: does robots.txt disallow the site
                # ROOT for our UA group? The skip counts below only see URLs we found a
                # link or sitemap entry for, so a `Disallow: /` site with no sitemap and a
                # thin homepage withholds everything while counting almost nothing. This
                # answers the question directly and needs no threshold. None (not False)
                # when robots.txt was never parsed, so "unknown" stays distinct from "no".
                "robots_root_disallowed": self._robots_root_disallowed(),
                # WHY this crawl does or does not hold robots.txt rules (issue #97).
                #
                # `robots_root_disallowed` above answers "did the rules say no". This answers
                # the prior question -- "did we ever READ the rules" -- which had no signal at
                # all on the most common route. `seeding/robots_failed` only ever fired on a
                # transport failure; a site that answers 403 or 503 returns an ordinary
                # response whose body the parser discards, leaving rules unset (allow-all)
                # and nothing counted. Cloudflare 403s robots.txt routinely, so the existing
                # tripwire was silently incomplete exactly where it mattered most.
                #
                # OBSERVED, not a verdict, per this section's rule -- it changes no crawl
                # behaviour. Whether `unreadable` should stop a crawl rather than proceed
                # allow-all is the open posture question in #97.
                "robots_readability": self._robots_readability(),
                "skipped": {
                    "robots_disallowed": self.stats.get_value("robots_disallowed_skipped", 0),
                    # Assets (PDFs, images) under a disallowed path are FILES, not withheld
                    # pages -- kept separate so they never inflate the withheld signal.
                    "robots_disallowed_assets": self.stats.get_value(
                        "robots_disallowed_assets_skipped", 0
                    ),
                    "login_gated": self.stats.get_value("login_urls_skipped", 0),
                    "infra": self.stats.get_value("infra_urls_skipped", 0),
                    "facet_capped": self.stats.get_value("facet_urls_skipped", 0),
                    "nofollow_links": self.stats.get_value("nofollow_links_skipped", 0),
                    "meta_nofollow_pages": self.stats.get_value("meta_nofollow_pages", 0),
                },
                # robots.txt Crawl-delay. `honored_seconds` is what we actually paced at;
                # `requested_seconds` is what the site asked for. They differ when the ask
                # exceeded YOKO_CRAWL_MAX_ROBOTS_DELAY and we clamped -- which is the case
                # where a crawl finalizes partial and the operator needs to know why.
                "crawl_delay": {
                    "applied": self.stats.get_value("robots_crawl_delay_applied", 0),
                    "honored_seconds": self.stats.get_value("robots_crawl_delay_honored", 0),
                    "requested_seconds": self.stats.get_value("robots_crawl_delay_requested", 0),
                },
            },
        }
        if final:
            data["finished_at"] = datetime.now(timezone.utc).isoformat()

        # Atomic write: fixed temp path, then rename.
        tmp_path = self.status_file + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.status_file)
        except OSError:
            # Disk full or permissions — LoopingCall survives,
            # monitor task is the backstop for final status.
            pass
