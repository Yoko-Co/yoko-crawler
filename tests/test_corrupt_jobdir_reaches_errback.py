"""A frontier Scrapy cannot READ arrives as `spider_init_error` (#103 review).

This pins the premise the whole JOBDIR-retention design rests on, because the first draft
asserted the OPPOSITE and shipped a comment saying a corrupt frontier could not raise this
token. It can:

    ExecutionEngine.open_spider_async()      calls scheduler.open(spider)
                                             inside a try whose only except is CloseSpider
    Crawler.crawl()                          except Exception: ... raise
    -> the Deferred process.crawl() returns  ERRBACKS
    -> run_spider._spider_never_started      -> failure_reason="spider_init_error"
    -> job_manager keeps the JOBDIR

So the token means "the spider never opened", NOT "the frontier is innocent". `job_manager`
keeps the JOBDIR on it, and `strike_jobdir` is what stops that keep from re-reading a bad
frontier forever: one strike leaves it, a second discards it -- see
`TestUnopenableJobdirSelfHeals` in test_run_spider.py.

Driven in a SUBPROCESS with a hard timeout: opening a real engine installs a reactor, which
cannot be torn down and would poison the rest of the suite.
"""

import os
import subprocess
import sys
import textwrap

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = {**os.environ, "YOKO_REPO_ROOT": _REPO_ROOT}

# Raised out of Scheduler.open for each shape. Asserted so a future Scrapy that errbacks for
# an UNRELATED reason cannot keep this file green while the pinned premise has moved.
_EXPECTED_EXC = {
    "truncated-active-json": "JSONDecodeError",
    "priority-queue-mismatch": "ValueError",
}

# Two shapes of unreadable frontier that `reset_incompatible_jobdir` does NOT catch, both
# reachable on a real droplet. It compares queue SLOT LAYOUT and skips `active.json`, so what
# escapes it is corruption that leaves the layout intact:
#   truncated-active-json    -- `_write_dqs_state` uses a plain open("w") + json.dump, so a
#                               full disk or a kill mid-write truncates it (JSONDecodeError).
#   priority-queue-mismatch  -- an `active.json` written by a DIFFERENT
#                               SCHEDULER_PRIORITY_QUEUE: a list, where the default
#                               DownloaderAwarePriorityQueue wants a dict (ValueError). The
#                               direct analogue of #52 one level UP, and the layout check
#                               cannot see it -- the disk-queue slots are correctly shaped.
# Measured: `reset_incompatible_jobdir` returns False for both, and each driver asserts that
# in-process so the claim cannot go stale. (It DOES catch a Lifo-shaped FILE slot -- an earlier
# draft of this file used that shape and claimed it escaped, which was wrong.)
_CORRUPTION_SHAPES = ["truncated-active-json", "priority-queue-mismatch"]

_DRIVER = textwrap.dedent(
    '''
    import json, os, sys, tempfile
    sys.path.insert(0, os.environ["YOKO_REPO_ROOT"])
    from scrapy import Spider
    from scrapy.crawler import CrawlerProcess
    from run_spider import reset_incompatible_jobdir

    class Trivial(Spider):
        name = "trivial"
        async def start(self):
            return
            yield

    jobdir = tempfile.mkdtemp()
    qdir = os.path.join(jobdir, "requests.queue")
    os.makedirs(qdir)
    shape = sys.argv[1]
    with open(os.path.join(qdir, "active.json"), "w") as f:
        if shape == "truncated-active-json":
            f.write("[0, 1")          # killed / disk full mid-write
        else:
            json.dump([0], f)
    if shape == "priority-queue-mismatch":
        # A correctly Fifo-SHAPED (directory) slot, so the disk-queue layout check passes and
        # the frontier reaches Scrapy unscreened; the state one level up is what it rejects.
        os.makedirs(os.path.join(qdir, "0"))

    fired = {}
    process = CrawlerProcess(settings={
        "JOBDIR": jobdir,
        "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
        "LOG_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
    })
    d = process.crawl(Trivial)
    d.addErrback(lambda f: fired.update(exc=type(f.value).__name__))
    process.start()
    print("ERRBACK:%s:%s" % (bool(fired), fired.get("exc")))
    print("PREFLIGHT_CATCHES:%s" % reset_incompatible_jobdir(
        jobdir, disk_queue="scrapy.squeues.PickleFifoDiskQueue"))
    '''
)


@pytest.mark.parametrize("shape", _CORRUPTION_SHAPES)
def test_an_unreadable_frontier_errbacks_the_crawl_deferred(shape, tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)

    result = subprocess.run(
        [sys.executable, str(driver), shape],
        capture_output=True,
        text=True,
        timeout=120,
        env=_ENV,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "PREFLIGHT_CATCHES:False" in result.stdout, (
        f"`reset_incompatible_jobdir` now catches the {shape!r} shape, so this case no longer "
        "demonstrates a frontier that reaches Scrapy unscreened. Pick a shape it still misses, "
        "or retire the case.\n"
        f"stdout={result.stdout!r}"
    )
    assert f"ERRBACK:True:{_EXPECTED_EXC[shape]}" in result.stdout, (
        f"a JOBDIR corrupted as {shape!r} no longer errbacks the crawl Deferred with "
        f"{_EXPECTED_EXC[shape]}.\n"
        "If Scrapy now handles this internally, `spider_init_error` really would mean 'the "
        "frontier is innocent' and job_manager's keep would be safe on its own -- but verify "
        "that before relaxing `strike_jobdir`, because the alternative reading is "
        "that the failure moved somewhere QUIETER.\n"
        f"stdout={result.stdout!r} stderr={result.stderr[-1500:]!r}"
    )


def test_a_readable_frontier_does_not_errback(tmp_path):
    """The control. Without it, a driver that failed for an unrelated reason -- a bad import,
    a settings typo -- would report ERRBACK:True and this file would pass while proving
    nothing about corrupt frontiers."""
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER.replace('f.write("[0, 1")', 'json.dump([], f)'))

    result = subprocess.run(
        [sys.executable, str(driver), "truncated-active-json"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_ENV,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "ERRBACK:False:" in result.stdout, (
        "an INTACT frontier errbacked the crawl Deferred, so the parametrized tests above "
        "are not measuring frontier corruption at all.\n"
        f"stdout={result.stdout!r} stderr={result.stderr[-1500:]!r}"
    )
