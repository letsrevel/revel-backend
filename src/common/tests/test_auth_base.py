"""Tests for BaseJWTAuth observability context binding."""

import pytest
import structlog
from django.test import RequestFactory, override_settings
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth

pytestmark = pytest.mark.django_db


@override_settings(FEATURE_OBSERVABILITY=True)
def test_jwt_auth_binds_user_id_contextvar(user: RevelUser) -> None:
    """Successful JWT auth binds user_id into the structlog request context.

    JWT auth runs at the view layer, after StructlogContextMiddleware has bound
    the request context (where request.user is still anonymous) — so the auth
    layer is responsible for making API request logs attributable to a user.
    """
    token = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")

    structlog.contextvars.clear_contextvars()
    try:
        authenticated = I18nJWTAuth().authenticate(request, token)

        assert authenticated == user
        assert structlog.contextvars.get_contextvars().get("user_id") == str(user.pk)
    finally:
        structlog.contextvars.clear_contextvars()


@override_settings(FEATURE_OBSERVABILITY=False)
def test_jwt_auth_skips_binding_when_observability_disabled(user: RevelUser) -> None:
    """With observability disabled, nothing is bound (contextvars are never cleared either)."""
    token = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")

    structlog.contextvars.clear_contextvars()
    try:
        authenticated = I18nJWTAuth().authenticate(request, token)

        assert authenticated == user
        assert "user_id" not in structlog.contextvars.get_contextvars()
    finally:
        structlog.contextvars.clear_contextvars()


def test_failed_jwt_auth_binds_nothing(user: RevelUser) -> None:
    """An invalid token must not bind a user_id."""
    request = RequestFactory().get("/", HTTP_AUTHORIZATION="Bearer not-a-token")

    structlog.contextvars.clear_contextvars()
    try:
        with pytest.raises(Exception):  # noqa: B017 - ninja_jwt raises its own error types
            I18nJWTAuth().authenticate(request, "not-a-token")

        assert "user_id" not in structlog.contextvars.get_contextvars()
    finally:
        structlog.contextvars.clear_contextvars()


def test_access_token_issued_before_credential_reset_is_refused(user: RevelUser) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from common.auth_base import SessionRevoked

    token = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    user.credentials_changed_at = timezone.now() + timedelta(seconds=2)
    user.save(update_fields=["credentials_changed_at"])
    request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")

    with pytest.raises(SessionRevoked):
        I18nJWTAuth().authenticate(request, token)


def test_access_token_issued_in_the_same_second_as_reset_is_accepted(user: RevelUser) -> None:
    from django.utils import timezone

    user.credentials_changed_at = timezone.now()
    user.save(update_fields=["credentials_changed_at"])
    token = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert I18nJWTAuth().authenticate(request, token) == user
