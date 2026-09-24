"""The developer guide's first-party-only route table must match the code, in both directions.

Pinning a single route session-only is a one-line ``auth=I18nJWTAuth()`` edit inside a controller
that otherwise accepts app tokens, so it is the part of the docs most likely to drift silently.
Whole controllers off the surface are described by area in the guide and are not checked here.
"""

import re
from pathlib import Path

from common.authentication import ScopedJWTAuth
from oauth.tests.test_scope_coverage import APP_TOKEN_CONTROLLERS, ROUTES

GUIDE = Path(__file__).resolve().parents[3] / "docs" / "developer-guide" / "oauth.md"
SECTION_START = "### Single routes inside app-token controllers"
SECTION_END = '!!! note "Why invitation links are first-party only"'
ROUTE_SPAN = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE) /[^`]*)`")


def _documented() -> set[str]:
    """Every ``METHOD /path`` code span in the guide's single-routes table."""
    text = GUIDE.read_text()
    section = text[text.index(SECTION_START) : text.index(SECTION_END)]
    return set(ROUTE_SPAN.findall(section))


def _pinned() -> set[str]:
    """Routes that require a first-party JWT inside a controller that accepts app tokens.

    ``auth is None`` routes (registration, password reset) are public, not first-party-only.
    """
    return {
        r.label
        for r in ROUTES
        if r.controller is not None
        and r.controller.__name__ in APP_TOKEN_CONTROLLERS
        and r.auth is not None
        and not isinstance(r.auth, ScopedJWTAuth)
    }


def test_the_pinned_set_is_not_empty() -> None:
    """A guard over an empty set asserts nothing (R-15)."""
    assert len(_pinned()) >= 20, sorted(_pinned())


def test_every_pinned_route_is_documented() -> None:
    """A route pinned session-only must be listed, or third-party developers find it by trial."""
    missing = sorted(_pinned() - _documented())
    assert not missing, f"Add these to the single-routes table in {GUIDE.name}: {missing}"


def test_every_documented_route_is_still_pinned() -> None:
    """A listed route that now accepts app tokens (or no longer exists) makes the guide lie."""
    stale = sorted(_documented() - _pinned())
    assert not stale, f"Remove these from the single-routes table in {GUIDE.name}: {stale}"
