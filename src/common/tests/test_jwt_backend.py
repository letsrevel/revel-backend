"""ninja-jwt tokens keep verifying across a SECRET_KEY rotation."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.exceptions import TokenBackendError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.jwt_backend import FallbackTokenBackend

pytestmark = pytest.mark.django_db


def _me(access: str) -> int:
    return Client(HTTP_AUTHORIZATION=f"Bearer {access}").get(reverse("api:me")).status_code


def _refresh(refresh: str) -> int:
    return (
        Client()
        .post(reverse("api:token_refresh"), data=orjson.dumps({"refresh": refresh}), content_type="application/json")
        .status_code
    )


def test_backend_is_installed() -> None:
    import ninja_jwt.state as state

    assert isinstance(state.token_backend, FallbackTokenBackend)


def test_access_token_signed_with_old_key(user: RevelUser, settings: t.Any) -> None:
    old = settings.SECRET_KEY
    access = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    settings.SECRET_KEY = "brand-new-secret"
    settings.SECRET_KEY_FALLBACKS = [old]
    assert _me(access) == 200
    settings.SECRET_KEY_FALLBACKS = []
    assert _me(access) == 401


def test_refresh_signed_with_old_key(user: RevelUser, settings: t.Any) -> None:
    old = settings.SECRET_KEY
    refresh = str(RefreshToken.for_user(user))
    settings.SECRET_KEY = "brand-new-secret"
    settings.SECRET_KEY_FALLBACKS = []
    assert _refresh(refresh) == 401
    settings.SECRET_KEY_FALLBACKS = [old]
    assert _refresh(refresh) == 200


def test_garbage_maps_to_token_backend_error() -> None:
    import ninja_jwt.state as state

    with pytest.raises(TokenBackendError):
        state.token_backend.decode("not-a-token")
