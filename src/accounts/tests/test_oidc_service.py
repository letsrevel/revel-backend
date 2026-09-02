"""Tests for discovery, state handling and the authorization URL (accounts.service.oidc)."""

import typing as t
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.core.cache import cache
from django.http import Http404

from accounts.exceptions import OIDCLoginError
from accounts.service import oidc
from revel.oidc_config import OIDCProviderConfig

GOOGLE = OIDCProviderConfig(
    key="google",
    name="Google",
    issuer="https://accounts.google.com",
    client_id="cid",
    client_secret="sec",
)

DISCOVERY_DOC = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
}


@pytest.fixture
def providers(settings: t.Any) -> None:
    settings.OIDC_PROVIDERS = (GOOGLE,)
    settings.BASE_URL = "https://api.example.test"


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route httpx through a MockTransport that serves the discovery doc; records requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=DISCOVERY_DOC)
        return httpx.Response(404)

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_get_provider(providers: None) -> None:
    assert oidc.get_provider("google") == GOOGLE
    with pytest.raises(Http404):
        oidc.get_provider("nope")
    assert oidc.list_providers() == [GOOGLE]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/events/x?y=1", "/events/x?y=1"),
        ("//evil.test", "/"),
        ("/\\evil.test", "/"),
        ("https://evil.test", "/"),
        ("javascript:alert(1)", "/"),
        ("events/x", "/"),
        ("/\t/evil.test", "/"),
        ("/\n/evil.test", "/"),
        ("/\r/evil.test", "/"),
        ("/events/x#frag", "/events/x#frag"),
    ],
)
def test_safe_return_url(raw: str | None, expected: str) -> None:
    assert oidc.safe_return_url(raw) == expected


def test_discovery_is_cached(mock_http: list[httpx.Request]) -> None:
    assert oidc.discovery(GOOGLE)["token_endpoint"] == DISCOVERY_DOC["token_endpoint"]
    assert oidc.discovery(GOOGLE)["token_endpoint"] == DISCOVERY_DOC["token_endpoint"]
    assert len(mock_http) == 1
    assert str(mock_http[0].url) == "https://accounts.google.com/.well-known/openid-configuration"


def test_discovery_issuer_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**DISCOVERY_DOC, "issuer": "https://evil.test"})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OIDCLoginError) as exc:
        oidc.discovery(GOOGLE)
    assert exc.value.code == "provider"


def test_discovery_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OIDCLoginError) as exc:
        oidc.discovery(GOOGLE)
    assert exc.value.code == "provider"


def test_begin_login_builds_url_and_stores_state(providers: None, mock_http: list[httpx.Request]) -> None:
    url = oidc.begin_login(GOOGLE, "/events/x")
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == DISCOVERY_DOC["authorization_endpoint"]
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "https://api.example.test/api/auth/oidc/google/callback"
    assert q["scope"] == "openid email profile"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32

    entry = cache.get(oidc._state_key(q["state"]))
    assert entry == {
        "provider": "google",
        "nonce": q["nonce"],
        "verifier": entry["verifier"],
        "return_url": "/events/x",
    }
    assert len(entry["verifier"]) >= 43


def test_begin_login_sanitises_return_url(providers: None, mock_http: list[httpx.Request]) -> None:
    url = oidc.begin_login(GOOGLE, "https://evil.test")
    state = parse_qs(urlparse(url).query)["state"][0]
    assert cache.get(oidc._state_key(state))["return_url"] == "/"
