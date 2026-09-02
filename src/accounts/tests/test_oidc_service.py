"""Tests for discovery, state handling and the authorization URL (accounts.service.oidc)."""

import typing as t
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from django.core.cache import cache
from django.http import Http404
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed

from accounts.exceptions import OIDCLoginError
from accounts.jwt import validate_oidc_login_token
from accounts.models import ExternalIdentity, RevelUser
from accounts.service import oidc
from accounts.tests.oidc_helpers import PUBLIC_KEY, make_id_token
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


def _fake_signing_key(provider: OIDCProviderConfig, id_token: str) -> t.Any:
    return PUBLIC_KEY


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oidc, "_signing_key", _fake_signing_key)


def _seed_state(state: str = "st", nonce: str = "nonce", provider: str = "google") -> None:
    cache.set(oidc._state_key(state), {"provider": provider, "nonce": nonce, "verifier": "v" * 64, "return_url": "/x"})


def test_pop_state_is_single_use(providers: None) -> None:
    _seed_state()
    assert oidc._pop_state(GOOGLE, "st")["return_url"] == "/x"
    with pytest.raises(OIDCLoginError) as exc:
        oidc._pop_state(GOOGLE, "st")
    assert exc.value.code == "state"


def test_pop_state_provider_mismatch(providers: None) -> None:
    _seed_state(provider="keycloak")
    with pytest.raises(OIDCLoginError) as exc:
        oidc._pop_state(GOOGLE, "st")
    assert exc.value.code == "state"


def test_exchange_code_posts_pkce_and_secret(providers: None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY_DOC)
        return httpx.Response(200, json={"id_token": "raw.id.token", "access_token": "at"})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert oidc._exchange_code(GOOGLE, "the-code", "the-verifier") == "raw.id.token"
    token_request = seen[-1]
    assert str(token_request.url) == DISCOVERY_DOC["token_endpoint"]
    body = parse_qs(token_request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["the-code"]
    assert body["code_verifier"] == ["the-verifier"]
    assert body["client_id"] == ["cid"]
    assert body["client_secret"] == ["sec"]
    assert body["redirect_uri"] == ["https://api.example.test/api/auth/oidc/google/callback"]


@pytest.mark.parametrize(
    "response", [httpx.Response(400, json={"error": "invalid_grant"}), httpx.Response(200, json={})]
)
def test_exchange_code_failures(providers: None, monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY_DOC)
        return response

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OIDCLoginError) as exc:
        oidc._exchange_code(GOOGLE, "c", "v")
    assert exc.value.code == "provider"


def test_verify_id_token_happy_path(providers: None, mock_http: list[httpx.Request], signing_key: None) -> None:
    claims = oidc._verify_id_token(GOOGLE, make_id_token(), "nonce")
    assert claims.sub == "sub-1"
    assert claims.email == "alice@example.com"
    assert claims.email_verified is True
    assert claims.given_name == "Alice"
    assert claims.locale == "de-AT"


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"nonce": "other"}, "state"),
        ({"audience": "someone-else"}, "provider"),
        ({"issuer": "https://evil.test"}, "provider"),
        ({"azp": "someone-else"}, "provider"),
        ({"exp": 1}, "provider"),
    ],
)
def test_verify_id_token_rejections(
    providers: None,
    mock_http: list[httpx.Request],
    signing_key: None,
    kwargs: dict[str, t.Any],
    expected_code: str,
) -> None:
    with pytest.raises(OIDCLoginError) as exc:
        oidc._verify_id_token(GOOGLE, make_id_token(**kwargs), "nonce")
    assert exc.value.code == expected_code


def test_verify_id_token_rejects_hs256(providers: None, mock_http: list[httpx.Request], signing_key: None) -> None:
    hs_token = make_id_token(alg="HS256", key="s" * 32)
    with pytest.raises(OIDCLoginError) as exc:
        oidc._verify_id_token(GOOGLE, hs_token, "nonce")
    assert exc.value.code == "provider"


def test_verify_id_token_missing_email_is_none(
    providers: None, mock_http: list[httpx.Request], signing_key: None
) -> None:
    claims = oidc._verify_id_token(GOOGLE, make_id_token(email=None), "nonce")
    assert claims.email is None
    assert claims.email_verified is True


def test_verify_id_token_malformed_email_claim(
    providers: None, mock_http: list[httpx.Request], signing_key: None
) -> None:
    with pytest.raises(OIDCLoginError) as exc:
        oidc._verify_id_token(GOOGLE, make_id_token(email="not-an-email"), "nonce")
    assert exc.value.code == "provider"


def test_signing_key_client_rebuilt_when_jwks_uri_changes(providers: None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen_uris: list[str] = []

    class FakeSigningKey:
        key = "fake-key"

    class FakePyJWKClient:
        def __init__(self, uri: str, **kwargs: t.Any) -> None:
            seen_uris.append(uri)

        def get_signing_key_from_jwt(self, id_token: str) -> t.Any:
            return FakeSigningKey()

    monkeypatch.setattr(jwt, "PyJWKClient", FakePyJWKClient)
    oidc._jwk_clients.clear()

    current_jwks_uri = {"value": DISCOVERY_DOC["jwks_uri"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={**DISCOVERY_DOC, "jwks_uri": current_jwks_uri["value"]})
        return httpx.Response(404)

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    try:
        oidc._signing_key(GOOGLE, "irrelevant.token.value")
        cache.delete(oidc._discovery_key(GOOGLE))
        current_jwks_uri["value"] = "https://www.googleapis.com/oauth2/v3/certs-new"
        oidc._signing_key(GOOGLE, "irrelevant.token.value")
    finally:
        oidc._jwk_clients.clear()

    assert seen_uris == [
        DISCOVERY_DOC["jwks_uri"],
        "https://www.googleapis.com/oauth2/v3/certs-new",
    ]


@pytest.fixture
def token_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery + token endpoint returning a freshly signed ID token for nonce 'nonce'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=DISCOVERY_DOC)
        return httpx.Response(200, json={"id_token": make_id_token()})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.django_db
def test_complete_login_returns_hand_off_token(providers: None, token_endpoint: None, signing_key: None) -> None:
    _seed_state()
    token = oidc.complete_login(GOOGLE, "code", "st")
    payload = validate_oidc_login_token(token)
    user = RevelUser.objects.get(id=payload.user_id)
    assert user.email == "alice@example.com"
    assert payload.return_url == "/x"


@pytest.mark.django_db
def test_complete_login_bad_state(providers: None, token_endpoint: None, signing_key: None) -> None:
    with pytest.raises(OIDCLoginError) as exc:
        oidc.complete_login(GOOGLE, "code", "unknown")
    assert exc.value.code == "state"


@pytest.mark.django_db
def test_redeem_login_token_once(providers: None, user: RevelUser) -> None:
    from accounts.jwt import create_oidc_login_token

    token = create_oidc_login_token(user_id=str(user.id), return_url="/x", jti="jti-1")
    pair, return_url = oidc.redeem_login_token(token)
    assert pair.username == user.username  # type: ignore[attr-defined]
    assert pair.access and pair.refresh
    assert return_url == "/x"
    with pytest.raises(HttpError) as exc:
        oidc.redeem_login_token(token)
    assert exc.value.status_code == 401


@pytest.mark.django_db
def test_redeem_login_token_inactive_user(inactive_user: RevelUser) -> None:
    from accounts.jwt import create_oidc_login_token

    token = create_oidc_login_token(user_id=str(inactive_user.id), return_url="/", jti="jti-2")
    with pytest.raises(AuthenticationFailed):
        oidc.redeem_login_token(token)


@pytest.mark.django_db
def test_redeem_login_token_invalid() -> None:
    with pytest.raises(AuthenticationFailed):
        oidc.redeem_login_token("garbage")


@pytest.mark.django_db
def test_list_and_unlink(providers: None, user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="1", email=user.email)
    assert [i.provider for i in oidc.list_identities(user)] == ["google"]
    oidc.unlink_identity(user, "google")
    assert not user.external_identities.exists()


@pytest.mark.django_db
def test_unlink_refused_when_stranded(user: RevelUser) -> None:
    user.set_unusable_password()
    user.save(update_fields=["password"])
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    with pytest.raises(HttpError) as exc:
        oidc.unlink_identity(user, "google")
    assert exc.value.status_code == 400
    assert user.external_identities.exists()


@pytest.mark.django_db
def test_unlink_allowed_with_second_identity(user: RevelUser) -> None:
    user.set_unusable_password()
    user.save(update_fields=["password"])
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    ExternalIdentity.objects.create(user=user, provider="keycloak", subject="1")
    oidc.unlink_identity(user, "google")
    assert [i.provider for i in user.external_identities.all()] == ["keycloak"]


@pytest.mark.django_db
def test_unlink_unknown_provider_404(user: RevelUser) -> None:
    with pytest.raises(Http404):
        oidc.unlink_identity(user, "google")


def test_provider_display_name(providers: None) -> None:
    assert oidc.provider_display_name("google") == "Google"
    assert oidc.provider_display_name("gone") == "Gone"
