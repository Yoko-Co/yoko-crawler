"""Tests for stats_extension.ProgressWriter (status file + SSRF-empty guard).

An all-403/blocked crawl is NOT failed here: the crawl completes and emits its 403
rows, and the consumer (yoko-corpus) owns the blocked-crawl policy (retry with browser
impersonation, then an honest "we couldn't read this" report). Only a crawl that fetched
NOTHING (SSRF guard dropped every host) is failed as a genuine empty result.
"""

import json

from stats_extension import ProgressWriter


class FakeStats:
    def __init__(self, values):
        self._values = values

    def get_value(self, key, default=0):
        return self._values.get(key, default)

    def get_stats(self):
        return self._values


def _write_and_read(tmp_path, stats_values, reason):
    status_file = str(tmp_path / "status.json")
    writer = ProgressWriter(FakeStats(stats_values), status_file)
    writer.spider_closed(spider=None, reason=reason)
    with open(status_file) as f:
        return json.load(f)


def test_all_403_completes_consumer_owns_policy(tmp_path):
    # A wholesale bot-block (every response 403) COMPLETES, emitting its 403 rows -- the
    # corpus reads the forbidden ratio to retry with impersonation / report honestly.
    # Failing here would deny it both. (Was: impersonated all-403 -> failed.)
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 50},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["error"] is None


def test_blocking_counts_surfaced(tmp_path):
    # Block/restriction observability: the status file exposes the challenge vs origin-403
    # split and a full status histogram, so the corpus/frontend can read the mix. Observed
    # counts, not a verdict -- the crawl still COMPLETES (policy stays with the consumer).
    data = _write_and_read(
        tmp_path,
        {
            "response_received_count": 60,
            "downloader/response_status_count/200": 40,
            "downloader/response_status_count/403": 20,
            "waf_challenge_count": 12,
            "origin_forbidden_count": 8,
        },
        reason="finished",
    )
    assert data["status"] == "completed"
    blocking = data["blocking"]
    assert blocking["waf_challenge_count"] == 12
    assert blocking["origin_forbidden_count"] == 8
    assert blocking["status_counts"] == {"200": 40, "403": 20}


def test_blocking_counts_default_to_zero(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 5, "downloader/response_status_count/200": 5},
        reason="finished",
    )
    assert data["blocking"]["waf_challenge_count"] == 0
    assert data["blocking"]["origin_forbidden_count"] == 0
    assert data["blocking"]["status_counts"] == {"200": 5}


def test_partial_403_completes(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 10},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["error"] is None


def test_all_redirects_completes(tmp_path):
    # A legit redirect-only (or 404-only) crawl has zero 200s -- it completes.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 50, "downloader/response_status_count/403": 0},
        reason="finished",
    )
    assert data["status"] == "completed"


def test_ssrf_blocked_into_emptiness_marked_failed(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"ssrf_guard/blocked": 3, "response_received_count": 0},
        reason="finished",
    )
    assert data["status"] == "failed"
    assert "SSRF guard" in data["error"]
    assert data["failure_reason"] == "ssrf_blocked"  # issue #44


def test_unreachable_target_marked_failed(tmp_path):
    # Fetched NOTHING because every request errored at the transport layer (DNS /
    # connection) -> a mistyped/unreachable address, surfaced as failed/unreachable
    # instead of a misleading "completed" with 0 pages (issue #44).
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 0, "downloader/exception_count": 5},
        reason="finished",
    )
    assert data["status"] == "failed"
    assert data["failure_reason"] == "unreachable"
    assert "unreachable" in data["error"]


def test_ssrf_wins_over_transport_exceptions_when_both_empty(tmp_path):
    # An SSRF-dropped host also raises a transport exception; the SSRF cause is the
    # more specific one and must win.
    data = _write_and_read(
        tmp_path,
        {"ssrf_guard/blocked": 2, "downloader/exception_count": 2, "response_received_count": 0},
        reason="finished",
    )
    assert data["failure_reason"] == "ssrf_blocked"


def test_empty_finish_without_cause_stays_completed(tmp_path):
    # 0 pages, no SSRF drops, no transport errors (e.g. everything robots-disallowed):
    # a genuinely empty finish -- unchanged behavior, still completed, no failure token.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 0},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["failure_reason"] is None


def test_transport_exceptions_with_pages_are_unaffected(tmp_path):
    # A real crawl that fetched pages but hit a few transport errors on stray links is
    # NOT reclassified -- only a wholly-empty crawl is.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 30, "downloader/exception_count": 3},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["failure_reason"] is None


def test_abnormal_close_gets_generic_failure_token(tmp_path):
    # A non-completed Scrapy close (OOM) that fetched pages fails with the generic token.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 10, "downloader/exception_count": 0},
        reason="memusage_exceeded",
    )
    assert data["status"] == "failed"
    assert data["failure_reason"] == "crawl_error"
    assert data["error"] == "memusage_exceeded"


def test_failure_reason_absent_on_success(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 20, "scheduler/enqueued": 20},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["failure_reason"] is None


def test_ssrf_block_with_fetched_pages_completes(tmp_path):
    # Dropped a stray internal link but fetched real pages -> the crawl is fine.
    data = _write_and_read(
        tmp_path,
        {"ssrf_guard/blocked": 1, "response_received_count": 20},
        reason="finished",
    )
    assert data["status"] == "completed"


def test_failure_reason_preserved(tmp_path):
    # A real non-completed close reason (e.g. OOM) still fails and is surfaced verbatim.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 10, "downloader/response_status_count/403": 2},
        reason="memusage_exceeded",
    )
    assert data["status"] == "failed"
    assert data["error"] == "memusage_exceeded"


def test_close_reason_surfaced_on_natural_finish(tmp_path):
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 20, "scheduler/enqueued": 20},
        reason="finished",
    )
    assert data["status"] == "completed"
    assert data["close_reason"] == "finished"


def test_close_reason_surfaced_on_safety_valve_stop(tmp_path):
    # A capped crawl reports "completed" but the close_reason marks it partial, and
    # discovered > crawled shows how much was left unfetched.
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 1200, "scheduler/enqueued": 5000},
        reason="closespider_timeout",
    )
    assert data["status"] == "completed"
    assert data["close_reason"] == "closespider_timeout"
    assert data["urls_crawled"] == 1200
    assert data["urls_discovered"] == 5000


class TestSeedingTripwire:
    """issue #52: a runtime signal for the bug class that silently disabled sitemap
    discovery. Scrapy renamed the seeding entry point, `WebsiteSpider.start_requests`
    became unreachable, Scrapy's default seeded instead -- and crawls kept succeeding,
    just with fewer pages. No exception, no failing test, no log line, for months.

    The invariant: our own seeding method counts every seed it emits, so `seeds_emitted
    == 0` on a crawl that fetched pages means something else did the seeding."""

    def test_status_file_reports_seeding_counters(self, tmp_path):
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "seeding/seeds_emitted": 2,
             "seeding/robots_fetched": 1, "seeding/sitemaps_fetched": 3,
             "seeding/start_urls_emitted": 1},
            reason="finished",
        )
        assert data["seeding"] == {"seeds_emitted": 2, "robots_fetched": 1,
                                   "sitemaps_fetched": 3, "start_urls_emitted": 1,
                                   "robots_failed": 0, "sitemap_probes_sent": 0,
                                   "sitemap_probes_found": 0,
                                   "sitemap_probes_disallowed": 0,
                                   "depth_reset_unsupported": 0}

    def test_status_file_reports_robots_failed(self, tmp_path):
        """A crawl that ran allow-all because robots.txt was UNREACHABLE must be
        distinguishable from one that read a permissive robots.txt (issue #76)."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "seeding/seeds_emitted": 2,
             "seeding/robots_fetched": 0, "seeding/start_urls_emitted": 1,
             "seeding/robots_failed": 1},
            reason="finished",
        )
        assert data["seeding"]["robots_failed"] == 1
        assert data["seeding"]["robots_fetched"] == 0

    def test_seeding_that_stops_after_robots_is_logged_as_an_error(self, tmp_path, caplog):
        """Phase-two tripwire (issue #76): robots.txt fetched, start URLs never emitted.
        The crawl closes `completed` with a one-row inventory, so nothing else catches it."""
        import logging
        with caplog.at_level(logging.ERROR):
            _write_and_read(
                tmp_path,
                {"response_received_count": 5000, "seeding/seeds_emitted": 1,
                 "seeding/robots_fetched": 5000, "seeding/start_urls_emitted": 0},
                reason="closespider_timeout",
            )
        assert "SEEDING STOPPED AFTER ROBOTS.TXT" in caplog.text

    def test_healthy_two_phase_seeding_is_not_flagged(self, tmp_path, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _write_and_read(
                tmp_path,
                {"response_received_count": 40, "seeding/seeds_emitted": 2,
                 "seeding/robots_fetched": 1, "seeding/start_urls_emitted": 1},
                reason="finished",
            )
        assert "SEEDING STOPPED AFTER ROBOTS.TXT" not in caplog.text

    def test_zero_seeds_on_a_crawl_that_fetched_pages_is_logged_as_an_error(self, tmp_path, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _write_and_read(
                tmp_path,
                {"response_received_count": 40, "seeding/seeds_emitted": 0},
                reason="finished",
            )
        assert "SEEDING DID NOT RUN" in caplog.text

    def test_healthy_crawl_does_not_trip_the_wire(self, tmp_path, caplog):
        import logging
        with caplog.at_level(logging.ERROR):
            _write_and_read(
                tmp_path,
                {"response_received_count": 40, "seeding/seeds_emitted": 2},
                reason="finished",
            )
        assert "SEEDING DID NOT RUN" not in caplog.text

    def test_empty_crawl_does_not_trip_the_wire(self, tmp_path, caplog):
        """A crawl that fetched nothing (SSRF-blocked, DNS failure) legitimately has no
        seeds counted; that is a different failure and already reported as such."""
        import logging
        with caplog.at_level(logging.ERROR):
            _write_and_read(
                tmp_path,
                {"response_received_count": 0, "seeding/seeds_emitted": 0},
                reason="finished",
            )
        assert "SEEDING DID NOT RUN" not in caplog.text

    def test_degraded_crawl_is_not_failed(self, tmp_path):
        """A link-following-only crawl is degraded, not worthless -- failing it would
        discard real pages over a defect the operator can fix and re-run."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "seeding/seeds_emitted": 0},
            reason="finished",
        )
        assert data["status"] == "completed"


class TestRestrictionsBlock:
    """Issue #74: the URL classes we deliberately don't fetch, and any Crawl-delay we paced
    at, have to leave Scrapy's stats. Until they did, a crawl the site itself had walled off
    was indistinguishable from a crawl of a genuinely small site."""

    def test_restrictions_are_reported(self, tmp_path):
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 2, "seeding/seeds_emitted": 2,
             "seeding/start_urls_emitted": 1,
             "robots_disallowed_skipped": 2345, "login_urls_skipped": 7,
             "infra_urls_skipped": 3, "facet_urls_skipped": 12,
             "nofollow_links_skipped": 4, "meta_nofollow_pages": 1,
             "robots_crawl_delay_applied": 1,
             "robots_crawl_delay_honored": 10.0,
             "robots_crawl_delay_requested": 15.0},
            reason="finished",
        )
        assert data["restrictions"]["skipped"] == {
            "robots_disallowed": 2345, "robots_disallowed_assets": 0, "login_gated": 7,
            "infra": 3, "facet_capped": 12, "nofollow_links": 4, "meta_nofollow_pages": 1,
        }
        assert data["restrictions"]["crawl_delay"] == {
            "applied": 1, "honored_seconds": 10.0, "requested_seconds": 15.0,
        }

    def test_the_gastro_shape_is_legible(self, tmp_path):
        """The case that motivated #74: a blanket `Disallow: /`. The crawl closes `finished`
        with a clean status histogram, so `restrictions` is the ONLY thing that can tell a
        consumer this is not an inventory."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 2, "seeding/seeds_emitted": 2,
             "seeding/robots_fetched": 1, "seeding/start_urls_emitted": 1,
             "downloader/response_status_count/200": 2,
             "robots_disallowed_skipped": 2345},
            reason="finished",
        )
        assert data["status"] == "completed"
        assert data["blocking"]["waf_challenge_count"] == 0
        assert data["blocking"]["origin_forbidden_count"] == 0
        skipped = data["restrictions"]["skipped"]["robots_disallowed"]
        assert skipped == 2345
        assert skipped > data["urls_crawled"] * 100, (
            "a consumer must be able to see the crawl covered a trivial fraction of the site"
        )

    def test_absent_stats_default_to_zero(self, tmp_path):
        """An unrestricted crawl reports a fully zeroed shape, never missing keys, so a
        consumer can read it without defensive lookups."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "seeding/seeds_emitted": 2,
             "seeding/start_urls_emitted": 1,
             # A crawl that seeded DID resolve robots.txt, so a readability outcome exists.
             # Omitting it now trips #97's not-recorded tripwire, which is the point of it.
             "robots_readability_outcome": "parsed", "robots_readability_status": 200},
            reason="finished",
        )
        assert set(data["restrictions"]) == {
            "skipped", "crawl_delay", "robots_root_disallowed", "robots_readability"}
        assert data["restrictions"]["robots_readability"] == {
            "outcome": "parsed", "final_status": 200, "cf_wall": False,
            "rules_from_state": False}
        # Never parsed -> None, NOT False: "we could not read the rules" must stay
        # distinct from "the rules allow us".
        assert data["restrictions"]["robots_root_disallowed"] is None
        assert all(v == 0 for v in data["restrictions"]["skipped"].values())
        assert all(v == 0 for v in data["restrictions"]["crawl_delay"].values())

    def test_robots_root_disallowed_is_reported(self, tmp_path):
        """The discovery-independent signal. A `Disallow: /` site with a thin homepage and
        no sitemap withholds everything while skipping almost nothing -- the counter can't
        see that, this can."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 2, "robots_root_disallowed": 1,
             "robots_disallowed_skipped": 4},
            reason="finished",
        )
        assert data["restrictions"]["robots_root_disallowed"] is True

    def test_assets_are_counted_apart_from_pages(self, tmp_path):
        """A `Disallow: /wp-content/uploads/` site with 600 linked PDFs is fully
        inventoried; those files must never read as withheld pages."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "robots_root_disallowed": 0,
             "robots_disallowed_skipped": 0,
             "robots_disallowed_assets_skipped": 600},
            reason="finished",
        )
        assert data["restrictions"]["skipped"]["robots_disallowed"] == 0
        assert data["restrictions"]["skipped"]["robots_disallowed_assets"] == 600
        assert data["restrictions"]["robots_root_disallowed"] is False

    def test_sitemap_probe_discovery_is_reported(self, tmp_path):
        """Issue #77: `found` > 0 means the site has a sitemap its robots.txt never named --
        coverage the crawl would previously have lost in silence."""
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 2400, "seeding/seeds_emitted": 2,
             "seeding/start_urls_emitted": 1, "seeding/robots_fetched": 1,
             "seeding/sitemaps_fetched": 12,
             "seeding/sitemap_probes_sent": 4, "seeding/sitemap_probes_found": 1},
            reason="finished",
        )
        assert data["seeding"]["sitemap_probes_sent"] == 4
        assert data["seeding"]["sitemap_probes_found"] == 1


def test_a_scheme_refused_empty_crawl_is_not_reported_as_unreachable(tmp_path):
    """#89 review: `ssrf_guard/blocked_scheme` is an SSRF-guard drop like any other. Left
    uncounted, a crawl the guard emptied by refusing non-http(s) URLs was classified
    `unreachable`, whose text tells the operator the address is wrong -- sending them to
    re-type a domain that was never the problem."""
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 0, "ssrf_guard/blocked_scheme": 3,
         "downloader/exception_count": 3},
        reason="finished",
    )
    assert data["failure_reason"] == "ssrf_blocked"
    assert data["status"] == "failed"



class TestRobotsReadabilityReporting:
    """The status-file half of crawler #97. `test_absent_stats_default_to_zero` above pins the
    all-defaults shape; hardcoding `_robots_readability()` to return exactly that default
    survived the whole 717-test suite (#97 review), so these drive REAL stats through it."""

    def test_a_refusal_reaches_the_status_file_intact(self, tmp_path):
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "seeding/seeds_emitted": 2,
             "seeding/start_urls_emitted": 1,
             "robots_readability_outcome": "unreadable",
             "robots_readability_status": 403,
             "robots_readability_cf_wall": True,
             "robots_readability_rules_from_state": False},
            reason="finished",
        )
        assert data["restrictions"]["robots_readability"] == {
            "outcome": "unreadable", "final_status": 403, "cf_wall": True,
            "rules_from_state": False}

    def test_a_parsed_crawl_reaches_the_status_file_intact(self, tmp_path):
        data = _write_and_read(
            tmp_path,
            {"response_received_count": 40, "seeding/seeds_emitted": 2,
             "seeding/start_urls_emitted": 1,
             "robots_readability_outcome": "parsed",
             "robots_readability_status": 200},
            reason="finished",
        )
        assert data["restrictions"]["robots_readability"]["outcome"] == "parsed"
        assert data["restrictions"]["robots_readability"]["final_status"] == 200

    def test_a_missing_outcome_after_seeding_is_not_recorded_not_unknown(self, tmp_path, caplog):
        """A broken instrument must not hide inside a legitimate outcome. `unknown` really
        means "the crawl ended before robots resolved"; if start URLs went out, the recorder
        should have run, and #52 is the standing lesson about what silence costs."""
        import logging
        with caplog.at_level(logging.ERROR):
            data = _write_and_read(
                tmp_path,
                {"response_received_count": 40, "seeding/seeds_emitted": 2,
                 "seeding/start_urls_emitted": 1},
                reason="finished",
            )
        assert data["restrictions"]["robots_readability"]["outcome"] == "not_recorded"
        assert "NOT RECORDED" in caplog.text

    def test_no_seeding_yet_stays_unknown_and_silent(self, tmp_path, caplog):
        """The legitimate case: robots.txt never resolved, so there is nothing to report and
        nothing is wrong. This must NOT trip the tripwire or it cries wolf every early poll."""
        import logging
        with caplog.at_level(logging.ERROR):
            data = _write_and_read(
                tmp_path, {"response_received_count": 0}, reason="finished")
        assert data["restrictions"]["robots_readability"]["outcome"] == "unknown"
        assert "NOT RECORDED" not in caplog.text


def test_job_manager_restrictions_default_matches_the_status_file_shape(tmp_path):
    """Two producers, one contract. `job_manager.get_status_response` carries its own zeroed
    `restrictions` literal for a crawl with no status file yet, and it silently omitted the
    new key (#97 review) -- so a consumer saw a different shape depending on WHICH producer
    answered, breaking the promise the code's own comment makes. Pinning both key sets
    together is the only thing that stops them drifting again."""
    import job_manager
    data = _write_and_read(
        tmp_path,
        {"response_received_count": 1, "seeding/seeds_emitted": 1,
         "seeding/start_urls_emitted": 1},
        reason="finished",
    )
    import inspect
    src = inspect.getsource(job_manager.JobManager.get_status_response)
    marker = '"restrictions": status_data.get("restrictions") or {'
    assert marker in src, "the fallback literal moved -- re-point this test"
    for key in data["restrictions"]:
        assert f'"{key}"' in src, (
            f"job_manager's zeroed restrictions default is missing {key!r}, so "
            f"GET /crawl/{{id}} returns a different shape than the status file"
        )
    for key in data["restrictions"]["robots_readability"]:
        assert f'"{key}"' in src, f"readability sub-key {key!r} missing from the fallback"


class TestSeedingIncomplete:
    """Issue #102: seeding is two-phase, and a crawl that completed phase one and never
    reached phase two fetched robots.txt and NO page of the site, yet reported `completed`.
    Every existing empty-crawl guard sits inside `if response_received_count == 0`, and
    robots.txt answering makes that 1, so none of them can see this shape.

    The flip must PROVE its own claim, which the first cut did not: review drove the real
    ProgressWriter and got a session reporting 4,000 pages back as failed."""

    def _closed(self, tmp_path, stats, reason="finished"):
        return _write_and_read(tmp_path, stats, reason=reason)

    # A crawl that read robots.txt, probed for sitemaps, and never reached a page.
    _SEEDING_ONLY = {
        "response_received_count": 3,
        "seeding/seeds_emitted": 1,
        "seeding/robots_fetched": 1,
        "seeding/sitemap_probes_missed": 2,
        "seeding/start_urls_emitted": 0,
        "robots_readability_outcome": "parsed",
    }

    def test_seeding_only_traffic_is_failed(self, tmp_path):
        data = self._closed(tmp_path, dict(self._SEEDING_ONLY))
        assert data["status"] == "failed"
        assert data["failure_reason"] == "seeding_incomplete"
        assert "not an inventory" in (data["error"] or "")

    def test_a_session_that_fetched_pages_is_never_failed(self, tmp_path):
        """THE false positive, reproduced by review at 4,000 pages. yoko-corpus runs one
        logical crawl as N sessions, so session 2+ restores a full frontier and fetches from
        it -- with `start_urls_emitted == 0` if phase two did not run THIS session. And the
        `completed` gate does not help, because `closespider_timeout`/`closespider_itemcount`
        are COMPLETED reasons and are how every session of that path ends."""
        for reason in ("finished", "closespider_timeout", "closespider_itemcount"):
            data = self._closed(tmp_path, {
                **self._SEEDING_ONLY, "response_received_count": 4000,
            }, reason=reason)
            assert data["status"] == "completed", reason
            assert data["failure_reason"] is None, reason

    def test_one_page_beyond_seeding_is_enough_to_spare_it(self, tmp_path):
        """The boundary: seeding traffic + a single real page. Failing this would call a
        crawl that reached the site "no page of the site was fetched"."""
        data = self._closed(tmp_path, {
            **self._SEEDING_ONLY, "response_received_count": 4})
        assert data["status"] == "completed"

    def test_robots_that_never_responded_does_not_fire_it(self, tmp_path):
        """The error text says robots.txt was fetched, so the condition must check that.
        `seeds_emitted` is bumped before the request is even scheduled and proves nothing."""
        data = self._closed(tmp_path, {
            "response_received_count": 0,
            "seeding/seeds_emitted": 1,
            "seeding/robots_fetched": 0,
            "seeding/start_urls_emitted": 0,
        })
        assert data["failure_reason"] != "seeding_incomplete"

    def test_a_robots_restricted_site_is_still_completed(self, tmp_path):
        """The design question. `_start_url_requests` is deliberately not routed through
        `_schedule`, so a `Disallow: /` site DOES emit and count its start URL.

        Note the row count: robots.txt is emitted as its own row too, so gastro.org's
        restricted crawl is TWO rows, not one. An earlier comment of mine said one, and a
        guard keyed on that number would have been wrong."""
        data = self._closed(tmp_path, {
            "response_received_count": 2,
            "seeding/seeds_emitted": 1,
            "seeding/robots_fetched": 1,
            "seeding/start_urls_emitted": 1,
            "robots_disallowed_skipped": 2345,
            "robots_root_disallowed": True,
            "robots_readability_outcome": "parsed",
        })
        assert data["status"] == "completed"
        assert data["failure_reason"] is None

    def test_the_kill_switch_leaves_the_log_but_not_the_failure(self, tmp_path, monkeypatch,
                                                               caplog):
        """Converting a #76-era log line into a client-visible failure with no production
        data on its frequency; rollback must not require redeploying a hand-managed venv."""
        import logging
        monkeypatch.setenv("YOKO_CRAWL_FAIL_SEEDING_INCOMPLETE", "0")
        with caplog.at_level(logging.ERROR):
            data = self._closed(tmp_path, dict(self._SEEDING_ONLY))
        assert data["status"] == "completed"
        assert data["failure_reason"] is None
        assert "SEEDING STOPPED AFTER ROBOTS.TXT" in caplog.text, \
            "the evidence must survive the switch being off"

    def test_a_more_specific_empty_crawl_cause_still_wins(self, tmp_path):
        """Ordering pin. Production does not actually produce this shape -- an SSRF-dropped
        or transport-failed robots seed errbacks to `robots_failed`, which emits the start
        URL -- but the precedence must hold regardless."""
        data = self._closed(tmp_path, {
            "response_received_count": 0,
            "seeding/seeds_emitted": 1,
            "seeding/robots_fetched": 1,
            "seeding/start_urls_emitted": 0,
            "downloader/exception_count": 3,
        })
        assert data["failure_reason"] == "unreachable"

    def test_ssrf_blocked_still_wins_too(self, tmp_path):
        data = self._closed(tmp_path, {
            "response_received_count": 0,
            "seeding/seeds_emitted": 1,
            "seeding/robots_fetched": 1,
            "seeding/start_urls_emitted": 0,
            "ssrf_guard/blocked": 1,
            "downloader/exception_count": 1,
        })
        assert data["failure_reason"] == "ssrf_blocked"

    def test_an_already_failed_crawl_keeps_its_own_reason(self, tmp_path):
        data = self._closed(tmp_path, dict(self._SEEDING_ONLY), reason="memusage_exceeded")
        assert data["status"] == "failed"
        assert data["failure_reason"] == "crawl_error"

    def test_a_normal_crawl_is_untouched(self, tmp_path):
        data = self._closed(tmp_path, {
            "response_received_count": 40,
            "seeding/seeds_emitted": 1,
            "seeding/robots_fetched": 1,
            "seeding/start_urls_emitted": 1,
            "robots_readability_outcome": "parsed",
        })
        assert data["status"] == "completed"
        assert data["failure_reason"] is None
