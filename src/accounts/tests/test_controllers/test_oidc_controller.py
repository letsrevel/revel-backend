"""Tests for the OIDC start/callback/exchange endpoints."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.exceptions import OIDCLoginError
from accounts.jwt import create_oidc_login_token
from accounts.models import RevelUser
from accounts.service.oidc import OIDCLoginStart
from common.models import SiteSettings
from revel.oidc_config import OIDCProviderConfig

pytestmark = pytest.mark.django_db

GOOGLE = OIDCProviderConfig(
    key="google", name="Google", issuer="https://accounts.google.com", client_id="c", client_secret="s"
)


@pytest.fixture(autouse=True)
def _settings(settings: t.Any, db: None) -> None:
    settings.OIDC_PROVIDERS = (GOOGLE,)
    # The redirects must follow the runtime SiteSettings value, not the env default.
    settings.FRONTEND_BASE_URL = "https://stale-env.example.test"
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://app.example.test"
    site.save(update_fields=["frontend_base_url"])


def _seed_state_cookie(client: Client, state: str = "s") -> None:
    """Visit the start route (with ``begin_login`` patched) so the state cookie lands on the client.

    The Django test client persists cookies across requests, mirroring how a real browser
    carries the ``oidc_state`` cookie from ``/start`` to ``/callback``.
    """
    with patch(
        "accounts.service.oidc.begin_login", return_value=OIDCLoginStart(url="https://idp.test/auth", state=state)
    ):
        client.get(reverse("api:oidc_start", kwargs={"provider": "google"}))


def test_start_redirects_to_idp(client: Client) -> None:
    start = OIDCLoginStart(url="https://idp.test/auth?x=1", state="s")
    with patch("accounts.service.oidc.begin_login", return_value=start) as begin:
        response = client.get(reverse("api:oidc_start", kwargs={"provider": "google"}), {"return_url": "/events/1"})
    assert response.status_code == 302
    assert response["Location"] == "https://idp.test/auth?x=1"
    begin.assert_called_once_with(GOOGLE, "/events/1")
    cookie = response.cookies["oidc_state"]
    assert cookie.value == "s"
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Lax"
    assert cookie["path"] == "/api/auth/oidc"


def test_start_unknown_provider_redirects_to_login(client: Client) -> None:
    """A stale or removed provider key is a browser navigation too: login page, not a JSON 404."""
    response = client.get(reverse("api:oidc_start", kwargs={"provider": "nope"}))
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_provider"


def test_start_provider_key_is_bounded(client: Client) -> None:
    """Keys are validated against the same pattern as ``OIDC_PROVIDERS`` (no unbounded str params)."""
    assert client.get(reverse("api:oidc_start", kwargs={"provider": "Google"})).status_code == 422
    assert client.get(reverse("api:oidc_start", kwargs={"provider": "a" * 65})).status_code == 422


def test_start_provider_failure_redirects_to_login(client: Client) -> None:
    """A discovery failure on /start is a browser navigation too — it must not answer with JSON."""
    with patch("accounts.service.oidc.begin_login", side_effect=OIDCLoginError("provider")):
        response = client.get(reverse("api:oidc_start", kwargs={"provider": "google"}))
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_provider"


def test_callback_success_redirects_to_frontend(client: Client) -> None:
    _seed_state_cookie(client, state="s")
    with patch("accounts.service.oidc.complete_login", return_value="one.time.token") as complete:
        response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "s"})
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/auth/callback?token=one.time.token"
    complete.assert_called_once_with(GOOGLE, "c", "s")
    assert response.cookies["oidc_state"]["max-age"] == 0


def test_callback_idp_error_redirects_denied(client: Client) -> None:
    with patch("accounts.service.oidc.complete_login") as complete:
        response = client.get(
            reverse("api:oidc_callback", kwargs={"provider": "google"}), {"error": "access_denied", "state": "s"}
        )
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_denied"
    complete.assert_not_called()


def test_callback_missing_code_is_state_error(client: Client) -> None:
    response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"state": "s"})
    assert response["Location"] == "https://app.example.test/login?error=oidc_state"


def test_callback_without_state_cookie_is_state_error(client: Client) -> None:
    with patch("accounts.service.oidc.complete_login") as complete:
        response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "s"})
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_state"
    complete.assert_not_called()


def test_callback_with_mismatched_state_cookie_is_state_error(client: Client) -> None:
    _seed_state_cookie(client, state="cookie-state")
    with patch("accounts.service.oidc.complete_login") as complete:
        response = client.get(
            reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "different-state"}
        )
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_state"
    complete.assert_not_called()


def test_callback_non_ascii_state_is_state_error(client: Client) -> None:
    """``secrets.compare_digest`` raises on non-ASCII input; that must not surface as a 500."""
    _seed_state_cookie(client, state="s")
    with patch("accounts.service.oidc.complete_login") as complete:
        response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "é"})
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_state"
    complete.assert_not_called()


def test_callback_oversized_state_rejected(client: Client) -> None:
    response = client.get(
        reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "s" * 257}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("code", ["state", "provider", "unverified_email", "no_email", "banned", "inactive"])
def test_callback_login_error_codes(client: Client, code: str) -> None:
    _seed_state_cookie(client, state="s")
    with patch("accounts.service.oidc.complete_login", side_effect=OIDCLoginError(code)):  # type: ignore[arg-type]
        response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "s"})
    assert response.status_code == 302
    assert response["Location"] == f"https://app.example.test/login?error=oidc_{code}"
    assert response.cookies["oidc_state"]["max-age"] == 0


def test_callback_unknown_provider_redirects_to_login(client: Client) -> None:
    _seed_state_cookie(client, state="s")
    response = client.get(reverse("api:oidc_callback", kwargs={"provider": "nope"}), {"code": "c", "state": "s"})
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/login?error=oidc_provider"
    assert response.cookies["oidc_state"]["max-age"] == 0


def test_exchange_returns_pair(client: Client, user: RevelUser) -> None:
    token = create_oidc_login_token(user_id=str(user.id), return_url="/events/1", jti="j1")
    response = client.post(
        reverse("api:oidc_exchange"), data=orjson.dumps({"token": token}), content_type="application/json"
    )
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"username", "access", "refresh", "return_url"}
    assert data["username"] == user.username
    assert data["return_url"] == "/events/1"


def test_exchange_accepts_token_carrying_longest_allowed_return_url(client: Client, user: RevelUser) -> None:
    """A 2048-char return_url passes safe_return_url; the JWT carrying it must still fit the token bound."""
    return_url = "/" + "x" * 2047
    token = create_oidc_login_token(user_id=str(user.id), return_url=return_url, jti="j-long")
    assert len(token) > 2048  # the old 2048 token cap would have rejected this at the schema
    response = client.post(
        reverse("api:oidc_exchange"), data=orjson.dumps({"token": token}), content_type="application/json"
    )
    assert response.status_code == 200
    assert response.json()["return_url"] == return_url


def test_exchange_is_single_use(client: Client, user: RevelUser) -> None:
    token = create_oidc_login_token(user_id=str(user.id), return_url="/", jti="j2")
    body = orjson.dumps({"token": token})
    assert client.post(reverse("api:oidc_exchange"), data=body, content_type="application/json").status_code == 200
    second = client.post(reverse("api:oidc_exchange"), data=body, content_type="application/json")
    assert second.status_code == 401
    assert "detail" in second.json()


def test_exchange_invalid_token_401(client: Client) -> None:
    response = client.post(
        reverse("api:oidc_exchange"), data=orjson.dumps({"token": "nope"}), content_type="application/json"
    )
    assert response.status_code == 401


def test_redirect_routes_not_in_openapi(client: Client) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/auth/oidc/{provider}/start" not in paths
    assert "/api/auth/oidc/{provider}/callback" not in paths
    assert "/api/auth/oidc/exchange" in paths
