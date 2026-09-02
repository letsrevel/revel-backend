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
from revel.oidc_config import OIDCProviderConfig

pytestmark = pytest.mark.django_db

GOOGLE = OIDCProviderConfig(
    key="google", name="Google", issuer="https://accounts.google.com", client_id="c", client_secret="s"
)


@pytest.fixture(autouse=True)
def _settings(settings: t.Any) -> None:
    settings.OIDC_PROVIDERS = (GOOGLE,)
    settings.FRONTEND_BASE_URL = "https://app.example.test"


def test_start_redirects_to_idp(client: Client) -> None:
    with patch("accounts.service.oidc.begin_login", return_value="https://idp.test/auth?x=1") as begin:
        response = client.get(reverse("api:oidc_start", kwargs={"provider": "google"}), {"return_url": "/events/1"})
    assert response.status_code == 302
    assert response["Location"] == "https://idp.test/auth?x=1"
    begin.assert_called_once_with(GOOGLE, "/events/1")


def test_start_unknown_provider_404(client: Client) -> None:
    response = client.get(reverse("api:oidc_start", kwargs={"provider": "nope"}))
    assert response.status_code == 404


def test_callback_success_redirects_to_frontend(client: Client) -> None:
    with patch("accounts.service.oidc.complete_login", return_value="one.time.token") as complete:
        response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "s"})
    assert response.status_code == 302
    assert response["Location"] == "https://app.example.test/auth/callback?token=one.time.token"
    complete.assert_called_once_with(GOOGLE, "c", "s")


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


@pytest.mark.parametrize("code", ["state", "provider", "unverified_email", "no_email", "banned", "inactive"])
def test_callback_login_error_codes(client: Client, code: str) -> None:
    with patch("accounts.service.oidc.complete_login", side_effect=OIDCLoginError(code)):  # type: ignore[arg-type]
        response = client.get(reverse("api:oidc_callback", kwargs={"provider": "google"}), {"code": "c", "state": "s"})
    assert response.status_code == 302
    assert response["Location"] == f"https://app.example.test/login?error=oidc_{code}"


def test_callback_unknown_provider_404(client: Client) -> None:
    response = client.get(reverse("api:oidc_callback", kwargs={"provider": "nope"}), {"code": "c", "state": "s"})
    assert response.status_code == 404


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
