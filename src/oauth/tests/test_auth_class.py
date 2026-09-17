"""Tests for ``ScopedJWTAuth``: session JWT first, then a django-oauth-toolkit app token.

These run against ``oauth.validator.RevelOAuth2Validator`` (``OAUTH2_VALIDATOR_CLASS``);
only ``verify_request`` is exercised, which our subclass does not touch.
"""

import datetime as dt
import typing as t
import uuid

import pytest
from django.test import RequestFactory
from django.utils import timezone
from ninja_jwt.exceptions import InvalidToken
from ninja_jwt.tokens import RefreshToken
from oauth2_provider.models import AccessToken, set_token_value
from oauth2_provider.settings import oauth2_settings
from pytest_django.fixtures import Settings

from accounts.models import RevelUser
from common.auth_base import PermissionDenied
from common.authentication import InvalidBearerToken, OAuthPrincipal, ScopedJWTAuth
from oauth.models import OAuthApplication

pytestmark = pytest.mark.django_db


def make_access_token(
    user: RevelUser | None, app: OAuthApplication | None, scopes: str, *, expires_in: int = 3600
) -> str:
    """Create a DOT access token row and return the raw bearer string.

    ``set_token_value`` honours ``COMPLIANT_BCP_RFC9700_TOKEN_STORAGE`` (on here), so
    the row stores only the SHA-256 checksum and the raw value never hits the DB.
    """
    # Hex only: DOT's own tokens come from oauthlib's UNICODE_ASCII_CHARACTER_SET, so a
    # realistic fake must contain no dots either (see the dispatch heuristic).
    raw = uuid.uuid4().hex
    token = AccessToken(
        user=user,
        application=app,
        scope=scopes,
        expires=timezone.now() + dt.timedelta(seconds=expires_in),
    )
    set_token_value(token, raw)
    token.save()
    return raw


def _auth(token: str, **flags: bool) -> tuple[t.Any, t.Any]:
    request = RequestFactory().get("/api/x", HTTP_AUTHORIZATION=f"Bearer {token}")
    return request, ScopedJWTAuth(**flags)(request)


def test_session_jwt_still_returns_user(user: RevelUser) -> None:
    access = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    request, result = _auth(access)
    assert result == user
    assert request.user == user


def test_app_token_returns_principal_and_sets_user(user: RevelUser, oauth_app: OAuthApplication) -> None:
    raw = make_access_token(user, oauth_app, "org:read profile")
    request, result = _auth(raw)
    assert isinstance(result, OAuthPrincipal)
    assert result.scopes == {"org:read", "profile"}
    assert result.client_id == oauth_app.client_id
    assert request.user == user


def test_expired_session_jwt_does_not_hit_dot(user: RevelUser, django_assert_num_queries: t.Any) -> None:
    """A JWT-shaped bearer is never offered to DOT, so an expired one fails without a query."""
    # ``access_token`` is a property that mints a FRESH token on every access, so it must
    # be bound once — expiring ``refresh.access_token`` inline would mutate a throwaway.
    access = RefreshToken.for_user(user).access_token  # type: ignore[attr-defined]
    access.set_exp(lifetime=dt.timedelta(seconds=-10))
    with django_assert_num_queries(0), pytest.raises(InvalidToken):
        _auth(str(access))


def test_registration_token_is_refused(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """A DCR registration access token is not an API credential (spec §7.2)."""
    raw = make_access_token(user, oauth_app, oauth2_settings.DCR_REGISTRATION_SCOPE)
    with pytest.raises(InvalidBearerToken):
        _auth(raw)


def test_expired_app_token_is_refused(user: RevelUser, oauth_app: OAuthApplication) -> None:
    raw = make_access_token(user, oauth_app, "org:read", expires_in=-10)
    with pytest.raises(InvalidBearerToken):
        _auth(raw)


def test_userless_token_is_refused(oauth_app: OAuthApplication) -> None:
    raw = make_access_token(None, oauth_app, "org:read")
    with pytest.raises(InvalidBearerToken):
        _auth(raw)


def test_app_less_token_is_refused(user: RevelUser) -> None:
    """DOT's ``application`` FK is nullable, but such a token is not an app credential."""
    raw = make_access_token(user, None, "org:read")
    with pytest.raises(InvalidBearerToken):
        _auth(raw)


def test_inactive_app_is_refused(user: RevelUser, oauth_app: OAuthApplication) -> None:
    raw = make_access_token(user, oauth_app, "org:read")
    oauth_app.is_active = False
    oauth_app.save(update_fields=["is_active"])
    with pytest.raises(InvalidBearerToken):
        _auth(raw)


def test_inactive_user_is_refused(user: RevelUser, oauth_app: OAuthApplication) -> None:
    raw = make_access_token(user, oauth_app, "org:read")
    user.is_active = False
    user.save(update_fields=["is_active"])
    with pytest.raises(InvalidBearerToken):
        _auth(raw)


def test_401_carries_resource_metadata(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """A non-JWT bearer falls through to DOT and is refused with the RFC 9728 challenge."""
    with pytest.raises(InvalidBearerToken) as exc:
        _auth("not-a-real-token")
    assert exc.value.www_authenticate == (
        'Bearer error="invalid_token", resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
    )


def test_requires_verified_email_applies_to_app_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    user.email_verified = False
    user.save(update_fields=["email_verified"])
    raw = make_access_token(user, oauth_app, "org:read")
    with pytest.raises(PermissionDenied):
        _auth(raw, requires_verified_email=True)


def test_staff_flag_refuses_app_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """An app token is never a staff credential, whatever the user's own flags (spec §7.2)."""
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    raw = make_access_token(user, oauth_app, "org:read")
    with pytest.raises(InvalidBearerToken):
        _auth(raw, is_staff=True)
    with pytest.raises(InvalidBearerToken):
        _auth(raw, is_superuser=True)


def test_provider_disabled_skips_dot(settings: Settings, user: RevelUser, oauth_app: OAuthApplication) -> None:
    """With no signing key the bearer is never offered to DOT: ninja-jwt's failure stands."""
    raw = make_access_token(user, oauth_app, "org:read")
    settings.OIDC_SIGNING_KEY_PATH = ""
    with pytest.raises(InvalidToken):
        _auth(raw)


def _last_used(app: OAuthApplication) -> dt.datetime | None:
    """Re-read ``last_used_at`` from the DB (a helper so mypy does not narrow the field)."""
    app.refresh_from_db(fields=["last_used_at"])
    return app.last_used_at


def test_last_used_at_is_bumped_once_per_window(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """Connected Apps needs a last-used timestamp; the window keeps it off every request."""
    assert _last_used(oauth_app) is None
    raw = make_access_token(user, oauth_app, "org:read")
    _auth(raw)
    first = _last_used(oauth_app)
    assert first is not None

    _auth(raw)
    assert _last_used(oauth_app) == first
