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
