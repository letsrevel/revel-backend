"""The ``throttled`` decorator that brings ninja's throttles to plain Django views."""

import json
import typing as t

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from common.throttling import OAuthTokenThrottle, throttled


def _request(rf: RequestFactory) -> HttpRequest:
    """A POST carrying the ``user`` attribute ``AuthenticationMiddleware`` would have set.

    ninja's ``AnonRateThrottle.get_cache_key`` reads ``request.user``, which only exists
    once the middleware has run; ``RequestFactory`` bypasses middleware.
    """
    request = rf.post("/o/token", REMOTE_ADDR="10.0.0.1")
    request.user = AnonymousUser()
    return request


@pytest.mark.django_db
def test_throttled_decorator_returns_429_after_limit(
    settings: t.Any, use_locmem_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The repo's root .env sets DISABLE_THROTTLING=True and decouple finds it from every
    # worktree, so without this the test would pass with throttling switched off (#936).
    settings.DISABLE_THROTTLING = False
    monkeypatch.setattr(OAuthTokenThrottle, "rate", "2/min")

    @throttled(OAuthTokenThrottle)
    def view(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    rf = RequestFactory()
    assert view(_request(rf)).status_code == 200
    assert view(_request(rf)).status_code == 200
    resp = view(_request(rf))
    assert resp.status_code == 429
    assert json.loads(resp.content)["error"] == "slow_down"
    assert int(resp["Retry-After"]) > 0


@pytest.mark.django_db
def test_throttled_decorator_passes_through_when_throttling_disabled(
    settings: t.Any, use_locmem_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.DISABLE_THROTTLING = True
    monkeypatch.setattr(OAuthTokenThrottle, "rate", "1/min")

    @throttled(OAuthTokenThrottle)
    def view(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    rf = RequestFactory()
    assert view(_request(rf)).status_code == 200
    assert view(_request(rf)).status_code == 200


@pytest.mark.django_db
def test_throttled_decorator_preserves_view_arguments(
    settings: t.Any, use_locmem_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOT's RFC 7592 management view takes a ``client_id`` URL kwarg."""
    settings.DISABLE_THROTTLING = False
    monkeypatch.setattr(OAuthTokenThrottle, "rate", "10/min")

    @throttled(OAuthTokenThrottle)
    def view(request: HttpRequest, client_id: str) -> HttpResponse:
        return HttpResponse(client_id)

    assert view(_request(RequestFactory()), client_id="abc").content == b"abc"


@pytest.mark.django_db
def test_throttle_applies_to_session_authenticated_callers(
    settings: t.Any, use_locmem_cache: None, monkeypatch: pytest.MonkeyPatch, django_user_model: t.Any
) -> None:
    """Stock ``AnonRateThrottle`` skips authenticated requests; the OAuth throttles must not.

    DOT's protocol views are plain Django views behind ``AuthenticationMiddleware``, so a
    session cookie makes ``request.user`` authenticated and would otherwise disable the limit
    on ``/o/token`` entirely.
    """
    settings.DISABLE_THROTTLING = False
    monkeypatch.setattr(OAuthTokenThrottle, "rate", "1/min")
    logged_in = django_user_model.objects.create_user(username="t@example.test", password="x")  # noqa: S106

    @throttled(OAuthTokenThrottle)
    def view(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    rf = RequestFactory()

    def _session_request() -> HttpRequest:
        request = rf.post("/o/token", REMOTE_ADDR="10.0.0.2")
        request.user = logged_in
        return request

    assert view(_session_request()).status_code == 200
    assert view(_session_request()).status_code == 429
