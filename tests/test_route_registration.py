"""Registration tripwire (issue #55): assert the API's routes are actually REGISTERED.

#52's lesson, generalised. Scrapy renamed its seeding entry point, `start_requests()` became
unreachable, and robots.txt/sitemap discovery silently stopped for months -- no exception, no
failing test, no log line. Three conditions had to hold, and removing any one catches it:

  1. a loose dependency pin let the extension point move,
  2. no test exercised the REAL entry point (ours hand-called the method), and
  3. no runtime signal distinguished "it didn't run" from "there was nothing to do".

`requirements.txt` now caps FastAPI, which removes (1). This removes (2) for the HTTP surface:
FastAPI routes are registered by DECORATOR, so a major version that changed decorator or
router semantics could leave the app with fewer routes and every other test would still pass
-- the ones that exercise a route would fail, but a route nobody tests would simply vanish.

Asserted against the OpenAPI schema rather than `app.routes`, deliberately: this FastAPI
version defers `include_router` behind `_IncludedRouter` objects that carry no `.path`, so
reading `app.routes` reports one anonymous entry and would have "passed" while proving
nothing. Building the schema forces resolution.
"""

from __future__ import annotations

from main import app

# Every path this service is contracted to expose. A path DISAPPEARING is the failure this
# guards; a new one appearing is fine and the assertion below tolerates it.
_EXPECTED_PATHS = {
    "/health",
    "/crawl",
    "/crawl/{job_id}",
    "/crawl/{job_id}/results",
}


def test_every_contracted_route_is_registered():
    registered = set(app.openapi()["paths"])
    missing = _EXPECTED_PATHS - registered
    assert not missing, (
        f"{len(missing)} route(s) vanished from the app: {sorted(missing)}. "
        "A FastAPI upgrade that changes decorator/router semantics can orphan routes with "
        "no error -- that is issue #52's failure class on the HTTP surface."
    )


def test_the_schema_actually_resolves_routes():
    """Guards the guard: if `openapi()` ever returned an empty or stub path set, the test
    above would pass vacuously against an app serving nothing."""
    assert len(app.openapi()["paths"]) >= len(_EXPECTED_PATHS)
