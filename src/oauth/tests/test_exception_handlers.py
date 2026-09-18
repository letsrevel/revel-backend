"""Tests for the oauth app's exception → HTTP mapping."""

import json
import typing as t

from django.http import HttpRequest

from common.authentication import InvalidBearerToken, MissingBearerToken
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


def test_invalid_bearer_token_is_401_with_challenge(settings: t.Any) -> None:
    """The 401 body is generic; the challenge points at the resource metadata (RFC 9728)."""
    settings.OAUTH_ISSUER = "http://testserver"
    resp = _render(InvalidBearerToken())
    assert resp.status_code == 401
    assert _body(resp) == {"detail": "Invalid or expired token."}
    assert resp["WWW-Authenticate"] == (
        'Bearer error="invalid_token", resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
    )


def test_invalid_bearer_token_omits_resource_metadata_when_unconfigured(settings: t.Any) -> None:
    """With no issuer configured there is no absolute metadata URI to advertise."""
    settings.OAUTH_ISSUER = ""
    resp = _render(InvalidBearerToken())
    assert resp["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_missing_bearer_token_challenge_has_no_error_attribute(settings: t.Any) -> None:
    """RFC 6750 §3.1: a credential-less 401 carries the scheme and the metadata pointer only."""
    settings.OAUTH_ISSUER = "http://testserver"
    # No handler of its own: the API dispatches by MRO, so the base class's handler renders it.
    resp = HANDLERS[InvalidBearerToken](HttpRequest(), MissingBearerToken())
    assert resp.status_code == 401
    assert _body(resp) == {"detail": "Authentication credentials were not provided."}
    assert resp["WWW-Authenticate"] == (
        'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
    )


def test_missing_bearer_token_challenge_is_bare_without_an_issuer(settings: t.Any) -> None:
    settings.OAUTH_ISSUER = ""
    assert HANDLERS[InvalidBearerToken](HttpRequest(), MissingBearerToken())["WWW-Authenticate"] == "Bearer"
