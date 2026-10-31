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


@pytest.fixture
def rotate(settings: t.Any) -> t.Iterator[t.Callable[[str, list[str]], None]]:
    """Simulate a restart under a new key: ninja-jwt's SIGNING_KEY is fixed at import, so move it too."""
    import ninja_jwt.state as state

    original = state.token_backend.signing_key

    def _rotate(new: str, fallbacks: list[str]) -> None:
        settings.SECRET_KEY = new
        settings.SECRET_KEY_FALLBACKS = fallbacks
        state.token_backend.signing_key = new

    yield _rotate
    state.token_backend.signing_key = original


def test_access_token_signed_with_old_key(
    user: RevelUser, settings: t.Any, rotate: t.Callable[[str, list[str]], None]
) -> None:
    old = settings.SECRET_KEY
    access = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    rotate("brand-new-secret", [old])
    assert _me(access) == 200
    rotate("brand-new-secret", [])
    assert _me(access) == 401


def test_refresh_signed_with_old_key(
    user: RevelUser, settings: t.Any, rotate: t.Callable[[str, list[str]], None]
) -> None:
    old = settings.SECRET_KEY
    refresh = str(RefreshToken.for_user(user))
    rotate("brand-new-secret", [])
    assert _refresh(refresh) == 401
    rotate("brand-new-secret", [old])
    assert _refresh(refresh) == 200


def test_garbage_maps_to_token_backend_error() -> None:
    import ninja_jwt.state as state

    with pytest.raises(TokenBackendError):
        state.token_backend.decode("not-a-token")


def test_backend_signing_key_is_tried_first(user: RevelUser, settings: t.Any) -> None:
    """Verifying follows the key the backend signs with, even if it differs from SECRET_KEY."""
    import ninja_jwt.state as state

    original = state.token_backend.signing_key
    state.token_backend.signing_key = "backend-only-key"
    try:
        token = state.token_backend.encode({"token_type": "access", "exp": 4102444800, "aud": settings.JWT_AUDIENCE})
        assert state.token_backend.decode(token)["token_type"] == "access"
    finally:
        state.token_backend.signing_key = original
    with pytest.raises(TokenBackendError):
        state.token_backend.decode(token)
