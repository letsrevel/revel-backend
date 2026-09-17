"""Tests for the oauth app's exception → HTTP mapping."""

import json
import typing as t

from django.http import HttpRequest

from oauth.exception_handlers import HANDLERS
from oauth.exceptions import (
    AppLimitReachedError,
    AuthorizationRequestError,
    InsufficientScopeError,
    OAuthProviderDisabledError,
)


def _render(exc: Exception) -> t.Any:
    return HANDLERS[type(exc)](HttpRequest(), exc)


def _body(response: t.Any) -> t.Any:
    return json.loads(response.content)


def test_disabled_is_404() -> None:
    resp = _render(OAuthProviderDisabledError())
    assert resp.status_code == 404
    assert _body(resp) == {"detail": "Not found."}


def test_authorization_request_error_carries_code() -> None:
    resp = _render(AuthorizationRequestError("invalid_scope", "nope"))
    assert resp.status_code == 400
    assert _body(resp) == {"detail": "nope", "error": "invalid_scope"}


def test_insufficient_scope_sets_www_authenticate() -> None:
    resp = _render(InsufficientScopeError("org:tickets"))
    assert resp.status_code == 403
    assert resp["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="org:tickets"'


def test_insufficient_scope_without_a_scope_omits_the_parameter() -> None:
    """An unscoped ``PermissionKey`` has no scope to name, and must never leak the key itself."""
    resp = _render(InsufficientScopeError())
    assert resp.status_code == 403
    assert resp["WWW-Authenticate"] == 'Bearer error="insufficient_scope"'


def test_app_limit_is_409() -> None:
    resp = _render(AppLimitReachedError())
    assert resp.status_code == 409
    assert _body(resp) == {"detail": "You have reached the maximum number of apps."}
