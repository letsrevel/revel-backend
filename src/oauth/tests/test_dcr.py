"""RFC 7591 / 7592 dynamic client registration through DOT's views (spec §6.1, §8.1)."""

import json
import typing as t

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.throttling import OAuthRegistrationThrottle
from oauth.models import OAuthApplication
from oauth.scopes import SCOPES

pytestmark = pytest.mark.django_db


def _register(client: Client, **metadata: t.Any) -> t.Any:
    body = {"client_name": "MCP Host", "redirect_uris": ["https://host.example/cb"], **metadata}
    return client.post("/o/register", data=json.dumps(body), content_type="application/json")


def test_public_client_registered(client: Client) -> None:
    resp = _register(client, token_endpoint_auth_method="none")
    assert resp.status_code == 201, resp.content
    data = resp.json()
    app = OAuthApplication.objects.get(client_id=data["client_id"])
    assert app.registration_source == OAuthApplication.RegistrationSource.DCR
    assert app.user_id is None and app.client_type == OAuthApplication.CLIENT_PUBLIC
    assert "client_secret" not in data
    # spec §6.1: a dynamic client may request the whole vocabulary; consent is the real gate.
    assert set(app.allowed_scopes) == set(SCOPES)


def test_confidential_client_gets_secret_once(client: Client) -> None:
    data = _register(client).json()
    assert data["client_secret"]
    app = OAuthApplication.objects.get(client_id=data["client_id"])
    assert app.client_secret != data["client_secret"]  # hashed at rest


def test_loopback_any_port_accepted(client: Client) -> None:
    resp = _register(client, redirect_uris=["http://127.0.0.1:51234/cb"], token_endpoint_auth_method="none")
    assert resp.status_code == 201, resp.content


def test_plain_http_and_custom_scheme_rejected(client: Client) -> None:
    for uri in ("http://host.example/cb", "myapp://cb"):
        resp = _register(client, redirect_uris=[uri], token_endpoint_auth_method="none")
        assert resp.status_code == 400, (uri, resp.status_code, resp.content)
        assert resp.json()["error"] == "invalid_client_metadata"


def test_client_credentials_grant_rejected(client: Client) -> None:
    """R-57: v1 excludes the client-credentials grant, so DCR may not request it."""
    resp = _register(client, grant_types=["client_credentials"], token_endpoint_auth_method="client_secret_basic")
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "invalid_client_metadata"


def test_session_logged_in_registrant_does_not_become_owner(client: Client, user: RevelUser) -> None:
    """R-58: only a Django session cookie can make ``request.user`` authenticated here."""
    client.force_login(user)
    resp = _register(client, token_endpoint_auth_method="none")
    assert resp.status_code == 201, resp.content
    assert OAuthApplication.objects.get(client_id=resp.json()["client_id"]).user_id is None


def test_bearer_registrant_does_not_become_owner(user: RevelUser) -> None:
    """Documentation, not a guard: ninja's auth never runs for DOT's plain-Django views, so
    ``request.user`` is anonymous for a JWT bearer regardless of the override (R-58)."""
    access = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    resp = _register(Client(HTTP_AUTHORIZATION=f"Bearer {access}"), token_endpoint_auth_method="none")
    assert resp.status_code == 201, resp.content
    assert OAuthApplication.objects.get(client_id=resp.json()["client_id"]).user_id is None


def test_management_endpoint_round_trip(client: Client) -> None:
    data = _register(client, token_endpoint_auth_method="none").json()
    headers = {"Authorization": f"Bearer {data['registration_access_token']}"}
    uri = data["registration_client_uri"]
    assert client.get(uri, headers=headers).status_code == 200
    assert client.delete(uri, headers=headers).status_code == 204
    assert not OAuthApplication.objects.filter(client_id=data["client_id"]).exists()


def test_daily_cap(settings: t.Any, client: Client) -> None:
    settings.OAUTH_DCR_DAILY_CAP = 1
    assert _register(client, token_endpoint_auth_method="none").status_code == 201
    resp = _register(client, token_endpoint_auth_method="none")
    assert resp.status_code == 429
    # Both mechanisms answer 429 with error "slow_down"; only the cap carries a description,
    # and only the throttle carries Retry-After. Discriminate, or the test proves nothing.
    assert resp.json()["error_description"]
    assert "Retry-After" not in resp


def test_daily_cap_is_not_spent_by_rejected_attempts(settings: t.Any, client: Client) -> None:
    """The cap is a budget of registrations; garbage must not lock out legitimate clients."""
    settings.OAUTH_DCR_DAILY_CAP = 1
    assert _register(client, redirect_uris=["myapp://cb"], token_endpoint_auth_method="none").status_code == 400
    assert _register(client, token_endpoint_auth_method="none").status_code == 201


def test_registration_is_throttled_per_ip(settings: t.Any, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    # See test_throttle_decorator: the root .env disables throttling for every test run.
    settings.DISABLE_THROTTLING = False
    monkeypatch.setattr(OAuthRegistrationThrottle, "rate", "1/hour")
    assert _register(client, token_endpoint_auth_method="none").status_code == 201
    resp = _register(client, token_endpoint_auth_method="none")
    assert resp.status_code == 429
    # See test_daily_cap: Retry-After and no description is what distinguishes the throttle
    # from the daily cap, which also answers 429 "slow_down".
    assert int(resp["Retry-After"]) > 0
    assert "error_description" not in resp.json()


def test_registration_throttle_ignores_session_authentication(
    settings: t.Any, client: Client, user: RevelUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session cookie must not buy an exemption from the per-IP limit.

    DOT's protocol views are plain Django views, so ``AuthenticationMiddleware`` populates
    ``request.user`` from the session — and stock ``AnonRateThrottle`` then declines to
    throttle at all. Anyone can obtain a session here: Google SSO auto-creates users for
    any domain (``settings/sso.py``).
    """
    settings.DISABLE_THROTTLING = False
    monkeypatch.setattr(OAuthRegistrationThrottle, "rate", "1/hour")
    client.force_login(user)
    assert _register(client, token_endpoint_auth_method="none").status_code == 201
    resp = _register(client, token_endpoint_auth_method="none")
    assert resp.status_code == 429
    assert resp["Retry-After"]
