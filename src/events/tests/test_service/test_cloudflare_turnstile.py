"""Tests for the Cloudflare Turnstile widget client (#1148). The HTTP layer is mocked; no network."""

import typing as t
from unittest.mock import patch

import httpx
import pytest

from events.exceptions import CloudflareAPIError
from events.service import cloudflare_turnstile as turnstile

WIDGET_URL = "https://api.cloudflare.com/client/v4/accounts/acct1/challenges/widgets/0xSITE"
WIDGET_RESULT: dict[str, t.Any] = {
    "sitekey": "0xSITE",
    "secret": "0xSECRET",
    "created_on": "2024-01-01T00:00:00Z",
    "modified_on": "2024-01-02T00:00:00Z",
    "deployed_via": "dashboard",
    "last_modified_via": "dashboard",
    "name": "Revel registration",
    "mode": "managed",
    "domains": ["letsrevel.io"],
    "region": "world",
    "bot_fight_mode": False,
    "offlabel": False,
    "ephemeral_id": False,
    "clearance_level": "no_clearance",
}


@pytest.fixture(autouse=True)
def turnstile_settings(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ACCOUNT_ID = "acct1"
    settings.TURNSTILE_SITE_KEY = "0xSITE"


def _response(status: int, body: t.Any) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://api.cloudflare.com/x"))


def _ok(result: t.Any) -> httpx.Response:
    return _response(200, {"success": True, "errors": [], "messages": [], "result": result})


@pytest.mark.parametrize(
    ("token", "account", "sitekey", "expected"),
    [
        ("tok", "acct1", "0xSITE", True),
        ("", "acct1", "0xSITE", False),
        ("tok", "", "0xSITE", False),
        ("tok", "acct1", "", False),
    ],
)
def test_is_configured_needs_token_account_and_site_key(
    settings: t.Any, token: str, account: str, sitekey: str, expected: bool
) -> None:
    settings.CLOUDFLARE_API_TOKEN = token
    settings.CLOUDFLARE_ACCOUNT_ID = account
    settings.TURNSTILE_SITE_KEY = sitekey
    assert turnstile.is_configured() is expected


def test_get_widget_fetches_by_site_key_and_parses_domains() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok(WIDGET_RESULT)) as request:
        widget = turnstile.get_widget()

    args, kwargs = request.call_args
    assert args == ("GET", WIDGET_URL)
    assert kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert widget.domains == ["letsrevel.io"]
    assert widget.name == "Revel registration"


def test_set_widget_domains_puts_the_whole_widget_with_only_domains_changed() -> None:
    widget = turnstile.TurnstileWidget.model_validate(WIDGET_RESULT)
    updated = {**WIDGET_RESULT, "domains": ["letsrevel.io", "events.example.org"]}
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok(updated)) as request:
        result = turnstile.set_widget_domains(widget, ["letsrevel.io", "events.example.org"])

    args, kwargs = request.call_args
    assert args == ("PUT", WIDGET_URL)
    assert kwargs["json"] == {
        "name": "Revel registration",
        "mode": "managed",
        "domains": ["letsrevel.io", "events.example.org"],
        "region": "world",
        "bot_fight_mode": False,
        "offlabel": False,
        "ephemeral_id": False,
        "clearance_level": "no_clearance",
    }  # every writable field preserved; the secret and server-managed fields are never sent back
    assert result.domains == ["letsrevel.io", "events.example.org"]


def test_error_envelope_raises_cloudflare_api_error() -> None:
    body = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}], "result": None}
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_response(403, body)):
        with pytest.raises(CloudflareAPIError) as exc_info:
            turnstile.get_widget()

    assert exc_info.value.status_code == 403
    assert "Authentication error" in str(exc_info.value)


def test_transport_error_raises_cloudflare_api_error() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", side_effect=httpx.ConnectTimeout("t")):
        with pytest.raises(CloudflareAPIError):
            turnstile.get_widget()


@pytest.mark.parametrize("result", [None, {"sitekey": "0xSITE"}])
def test_missing_or_malformed_widget_raises(result: t.Any) -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok(result)):
        with pytest.raises(CloudflareAPIError):
            turnstile.get_widget()
