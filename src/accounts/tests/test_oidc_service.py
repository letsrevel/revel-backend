"""Tests for discovery, state handling and the authorization URL (accounts.service.oidc)."""

import time
import typing as t
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from django.core.cache import cache
from django.http import Http404
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed
from ninja_jwt.token_blacklist.models import BlacklistedToken

from accounts.exceptions import OIDCLoginError
from accounts.jwt import create_oidc_login_token, validate_oidc_login_token
from accounts.models import ExternalIdentity, GlobalBan, RevelUser
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
        ("/" + "x" * 2048, "/"),
    ],
)
def test_safe_return_url(raw: str | None, expected: str) -> None:
    assert oidc.safe_return_url(raw) == expected


def test_discovery_is_cached(mock_http: list[httpx.Request]) -> None:
    assert oidc.discovery(GOOGLE).token_endpoint == DISCOVERY_DOC["token_endpoint"]
    assert oidc.discovery(GOOGLE).token_endpoint == DISCOVERY_DOC["token_endpoint"]
    assert len(mock_http) == 1
    assert str(mock_http[0].url) == "https://accounts.google.com/.well-known/openid-configuration"


def test_discovery_cache_is_keyed_on_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing ``OIDC_<KEY>_ISSUER`` for the same key must not keep serving the old issuer's endpoints."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        issuer = f"{request.url.scheme}://{request.url.host}"
        return httpx.Response(200, json={**DISCOVERY_DOC, "issuer": issuer})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    moved = OIDCProviderConfig(
        key="google", name="Google", issuer="https://idp2.example.test", client_id="cid", client_secret="sec"
    )

    oidc.discovery(GOOGLE)
    assert oidc.discovery(moved).issuer == "https://idp2.example.test"
    assert [r.url.host for r in seen] == ["accounts.google.com", "idp2.example.test"]


@pytest.mark.parametrize(
    "bad",
    [{"issuer": []}, {"issuer": None}, {"token_endpoint": ""}, {"jwks_uri": 5}, {"authorization_endpoint": None}],
)
def test_discovery_malformed_document_rejected(monkeypatch: pytest.MonkeyPatch, bad: dict[str, t.Any]) -> None:
    """Wrong-typed fields must be an OIDCLoginError (login-page redirect), never an AttributeError 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**DISCOVERY_DOC, **bad})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OIDCLoginError) as exc:
        oidc.discovery(GOOGLE)
    assert exc.value.code == "provider"


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
def test_discovery_insecure_endpoint_rejected_outside_debug(
    monkeypatch: pytest.MonkeyPatch, settings: t.Any, field: str
) -> None:
    """The client secret and PKCE verifier go to ``token_endpoint``; an ``http://`` one is refused unless DEBUG."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**DISCOVERY_DOC, field: "http://accounts.google.com/insecure"})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    settings.DEBUG = False
    with pytest.raises(OIDCLoginError) as exc:
        oidc.discovery(GOOGLE)
    assert exc.value.code == "provider"
    settings.DEBUG = True
    assert getattr(oidc.discovery(GOOGLE), field) == "http://accounts.google.com/insecure"


def test_redirect_uri_tolerates_trailing_slash_in_base_url(settings: t.Any) -> None:
    settings.BASE_URL = "https://api.example.test/"
    assert oidc._redirect_uri(GOOGLE) == "https://api.example.test/api/auth/oidc/google/callback"


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


def test_discovery_non_object_body_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OIDCLoginError) as exc:
        oidc.discovery(GOOGLE)
    assert exc.value.code == "provider"


def test_begin_login_builds_url_and_stores_state(providers: None, mock_http: list[httpx.Request]) -> None:
    start = oidc.begin_login(GOOGLE, "/events/x")
    parsed = urlparse(start.url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == DISCOVERY_DOC["authorization_endpoint"]
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "https://api.example.test/api/auth/oidc/google/callback"
    assert q["scope"] == "openid email profile"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32
    assert q["state"] == start.state

    entry = cache.get(oidc._state_key(q["state"]))
    assert entry == {
        "provider": "google",
        "nonce": q["nonce"],
        "verifier": entry["verifier"],
        "return_url": "/events/x",
    }
    assert len(entry["verifier"]) >= 43


def test_begin_login_sanitises_return_url(providers: None, mock_http: list[httpx.Request]) -> None:
    start = oidc.begin_login(GOOGLE, "https://evil.test")
    assert cache.get(oidc._state_key(start.state))["return_url"] == "/"


def test_begin_login_discovery_failure_writes_no_state(providers: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with patch.object(oidc.cache, "set") as cache_set, pytest.raises(OIDCLoginError):
        oidc.begin_login(GOOGLE, "/")
    cache_set.assert_not_called()


def test_state_matches_cookie() -> None:
    assert oidc.state_matches_cookie("s", "s") is True
    assert oidc.state_matches_cookie("s", "other") is False
    assert oidc.state_matches_cookie(None, "s") is False
    assert oidc.state_matches_cookie("s", "é") is False  # compare_digest would raise TypeError on this


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


def test_pop_state_loser_of_concurrent_delete_is_refused(providers: None) -> None:
    """Two callbacks racing on one state: the one whose ``cache.delete`` finds nothing must not proceed."""
    _seed_state()
    with patch.object(oidc.cache, "delete", return_value=False), pytest.raises(OIDCLoginError) as exc:
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
    "response",
    [httpx.Response(400, json={"error": "invalid_grant"}), httpx.Response(200, json={}), httpx.Response(200, json=[])],
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


def test_verify_id_token_tolerates_clock_skew(
    providers: None, mock_http: list[httpx.Request], signing_key: None
) -> None:
    """A token issued a few seconds ahead of our clock (observed from Google) must still verify."""
    token = make_id_token(iat=int(time.time()) + 10)
    claims = oidc._verify_id_token(GOOGLE, token, "nonce")
    assert claims.sub == "sub-1"


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
    token = create_oidc_login_token(user_id=str(user.id), return_url="/x", jti="jti-1")
    pair, return_url = oidc.redeem_login_token(token)
    assert pair.username == user.username  # type: ignore[attr-defined]
    assert pair.access and pair.refresh
    assert return_url == "/x"
    with pytest.raises(HttpError) as exc:
        oidc.redeem_login_token(token)
    assert exc.value.status_code == 401


@pytest.mark.django_db
def test_redeem_login_token_inactive_user_consumes_token(inactive_user: RevelUser) -> None:
    """The failure path must still burn the token — a rolled-back blacklist row would leave it redeemable."""
    token = create_oidc_login_token(user_id=str(inactive_user.id), return_url="/", jti="jti-2")
    with pytest.raises(AuthenticationFailed):
        oidc.redeem_login_token(token)
    assert BlacklistedToken.objects.filter(token__jti="jti-2").exists()


@pytest.mark.django_db
def test_redeem_login_token_rechecks_global_ban(user: RevelUser, superuser: RevelUser) -> None:
    """A ban landing between the callback and the exchange must block the login (mirrors reset_password)."""
    token = create_oidc_login_token(user_id=str(user.id), return_url="/", jti="jti-4")
    GlobalBan.objects.create(ban_type=GlobalBan.BanType.EMAIL, value=user.email, created_by=superuser)
    with pytest.raises(AuthenticationFailed):
        oidc.redeem_login_token(token)
    assert BlacklistedToken.objects.filter(token__jti="jti-4").exists()


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
def test_unlink_refused_when_password_blank(user: RevelUser) -> None:
    """Legacy IdP-created users have ``password == ""``, for which Django's own
    ``has_usable_password()`` returns True; the unlink guard must not be fooled by that."""
    user.password = ""
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


@pytest.mark.django_db
def test_unlink_removes_every_identity_for_provider(user: RevelUser) -> None:
    """A rotated ``sub`` at the IdP leaves two rows for one provider; both go, and the other provider stays."""
    ExternalIdentity.objects.create(user=user, provider="google", subject="old-sub")
    ExternalIdentity.objects.create(user=user, provider="google", subject="new-sub")
    ExternalIdentity.objects.create(user=user, provider="keycloak", subject="k")
    oidc.unlink_identity(user, "google")
    assert [i.provider for i in user.external_identities.all()] == ["keycloak"]


def test_provider_display_name(providers: None) -> None:
    assert oidc.provider_display_name("google") == "Google"
    assert oidc.provider_display_name("gone") == "Gone"
