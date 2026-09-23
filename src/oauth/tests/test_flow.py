"""End-to-end authorization-code + PKCE flow: consent, code, token, refresh, replay.

This is the only place the provider is exercised as a whole, so the assertions are the
feature's contract rather than illustrations.
"""

import base64
import hashlib
import secrets
import typing as t
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import pytest
from django.test.client import Client
from oauth2_provider.models import AccessToken, Grant, RefreshToken

from accounts.models import RevelUser
from oauth.models import OAuthApplication
from oauth.scopes import SCOPES

pytestmark = pytest.mark.django_db

REDIRECT_URI = "https://app.example/cb"


def pkce() -> tuple[str, str]:
    """Build a PKCE ``(verifier, S256 challenge)`` pair."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_query(app: OAuthApplication, challenge: str, scope: str, **extra: str) -> dict[str, str]:
    """The query parameters the frontend consent page forwards to the backend.

    An override with an empty value omits the parameter, which is how a client that sends no
    ``resource`` at all is expressed.
    """
    query = {
        "client_id": app.client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": "http://testserver",
        **extra,
    }
    return {key: value for key, value in query.items() if value != ""}


def authorize_url(app: OAuthApplication, challenge: str, scope: str, **extra: str) -> str:
    """URL for the consent endpoint, properly encoded (R-68).

    ``urlencode`` matters beyond tidiness: ``scope`` is space-separated, so a hand-joined
    query string is not what the real frontend sends and would not exercise the decoding
    oauthlib does.
    """
    return "/api/oauth/authorize?" + urlencode(authorize_query(app, challenge, scope, **extra))


def describe_consent(
    session_client: Client, app: OAuthApplication, challenge: str, scope: str, **extra: str
) -> dict[str, t.Any]:
    """GET the consent screen's contents (or its auto-approval redirect)."""
    response = session_client.get("/api/oauth/authorize", authorize_query(app, challenge, scope, **extra))
    assert response.status_code == 200, response.content
    return t.cast(dict[str, t.Any], response.json())


def decide(
    session_client: Client, app: OAuthApplication, challenge: str, scope: str, *, allow: bool, **extra: str
) -> str:
    """POST the consent decision and return the ``redirect_to`` the backend answers with.

    Approving fetches the consent ticket from ``describe`` first, exactly as the consent page
    does; the tests that probe the ticket itself build their POST inline instead.
    """
    body: dict[str, t.Any] = {"allow": allow}
    if allow:
        body["consent_ticket"] = describe_consent(session_client, app, challenge, scope, **extra)["consent_ticket"]
    response = session_client.post(
        authorize_url(app, challenge, scope, **extra),
        data=body,
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return t.cast(str, response.json()["redirect_to"])


def code_from(redirect_to: str) -> str:
    """Pull the authorization code out of a successful authorization redirect."""
    assert redirect_to.startswith(REDIRECT_URI + "?"), redirect_to
    return parse_qs(urlparse(redirect_to).query)["code"][0]


def run_code_flow(
    session_client: Client,
    client: Client,
    app: OAuthApplication,
    scope: str,
    *,
    secret: str | None = None,
    **extra: str,
) -> dict[str, t.Any]:
    """Drive consent → code → token exchange and return the token response body."""
    verifier, challenge = pkce()
    code = code_from(decide(session_client, app, challenge, scope, allow=True, **extra))
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": app.client_id,
        "code_verifier": verifier,
    }
    if secret:
        form["client_secret"] = secret
    token = client.post("/o/token", form)
    assert token.status_code == 200, token.content
    return t.cast(dict[str, t.Any], token.json())


@pytest.fixture
def confidential_secret(user: RevelUser) -> tuple[OAuthApplication, str]:
    """A confidential app plus the raw secret (the stored one is hashed)."""
    raw = "s3cret-" + secrets.token_urlsafe(16)
    app = OAuthApplication(
        user=user,
        name="C",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        client_secret=raw,
        redirect_uris=REDIRECT_URI,
        allowed_scopes=sorted(SCOPES),
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    app.full_clean()
    app.save()
    return app, raw


def test_happy_path_public_client(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication, user: RevelUser
) -> None:
    """R-63: the smoke target is ``/api/dashboard/organizations``, switched by Task 10.

    ``DashboardController`` is ``ScopedJWTAuth`` and its ``/organizations`` route carries
    ``RequireScope("org:read")``, so this is a real app-token request, not an anonymous one.
    """
    tokens = run_code_flow(session_client, client, public_oauth_app, "openid profile org:read offline_access")
    assert set(tokens) >= {"access_token", "refresh_token", "id_token", "expires_in", "scope"}
    claims = jwt.decode(tokens["id_token"], options={"verify_signature": False})
    assert claims["sub"] == str(user.pk) and claims["name"]
    me = client.get("/api/dashboard/organizations", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    assert me.status_code == 200, me.content


def test_no_offline_access_no_refresh_token(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    tokens = run_code_flow(session_client, client, public_oauth_app, "openid org:read")
    assert "refresh_token" not in tokens
    assert not RefreshToken.objects.exists()


def test_confidential_client_needs_secret(
    session_client: Client, client: Client, confidential_secret: tuple[OAuthApplication, str]
) -> None:
    """R-76: prove the secret is actually *required*, not merely accepted."""
    app, secret = confidential_secret
    verifier, challenge = pkce()
    code = code_from(decide(session_client, app, challenge, "org:read", allow=True))
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": app.client_id,
        "code_verifier": verifier,
    }
    without_secret = client.post("/o/token", form)
    assert without_secret.status_code in (400, 401), without_secret.content
    assert without_secret.json()["error"] == "invalid_client"
    tokens = run_code_flow(session_client, client, app, "org:read", secret=secret)
    assert tokens["access_token"]


def test_wrong_verifier_rejected(session_client: Client, client: Client, public_oauth_app: OAuthApplication) -> None:
    _, challenge = pkce()
    code = code_from(decide(session_client, public_oauth_app, challenge, "org:read", allow=True))
    resp = client.post(
        "/o/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": public_oauth_app.client_id,
            "code_verifier": "wrong",
        },
    )
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


def test_refresh_rotation_and_reuse_detection(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    tokens = run_code_flow(session_client, client, public_oauth_app, "org:read offline_access")
    first = tokens["refresh_token"]
    second = client.post(
        "/o/token",
        {"grant_type": "refresh_token", "refresh_token": first, "client_id": public_oauth_app.client_id},
    ).json()
    assert second["refresh_token"] != first
    replay = client.post(
        "/o/token",
        {"grant_type": "refresh_token", "refresh_token": first, "client_id": public_oauth_app.client_id},
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    family_dead = client.post(
        "/o/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": second["refresh_token"],
            "client_id": public_oauth_app.client_id,
        },
    )
    assert family_dead.status_code == 400


def test_scope_narrowing_refresh_still_rotates(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """Dropping ``offline_access`` on a refresh must not skip rotation.

    ``request.scopes`` on a refresh is the client's *requested* list, so the ``offline_access``
    gate used to pop the new refresh token, DOT then skipped its revoke block, and the presented
    token stayed live: replayable indefinitely without ever tripping reuse detection.
    """
    tokens = run_code_flow(session_client, client, public_oauth_app, "org:read org:events offline_access")
    first = tokens["refresh_token"]
    narrowed = client.post(
        "/o/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": first,
            "client_id": public_oauth_app.client_id,
            "scope": "org:read org:events",
        },
    )
    assert narrowed.status_code == 200, narrowed.content
    assert narrowed.json()["refresh_token"] != first
    assert RefreshToken.objects.get(token_checksum=hashlib.sha256(first.encode()).hexdigest()).revoked is not None
    replay = client.post(
        "/o/token",
        {"grant_type": "refresh_token", "refresh_token": first, "client_id": public_oauth_app.client_id},
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"


def test_foreign_resource_token_fails_audience(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """R-63: the only end-to-end proof that RFC 8707 resource restriction actually binds.

    A switched route is required: ``OptionalAuth`` answers *every* app token with a bare 401
    (it only knows session JWTs), so a public route would 401 a correctly bound token as well
    and could not tell the audience check apart from that.
    """
    verifier, challenge = pkce()
    redirect_to = decide(
        session_client, public_oauth_app, challenge, "org:read", allow=True, resource="https://other.example"
    )
    tokens = client.post(
        "/o/token",
        {
            "grant_type": "authorization_code",
            "code": code_from(redirect_to),
            "redirect_uri": REDIRECT_URI,
            "client_id": public_oauth_app.client_id,
            "code_verifier": verifier,
            "resource": "https://other.example",
        },
    ).json()
    resp = client.get("/api/dashboard/organizations", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    assert resp.status_code == 401 and 'error="invalid_token"' in resp["WWW-Authenticate"]


def test_grant_records_the_resource_indicator_as_a_list(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """RFC 8707: oauthlib ignores ``resource``, so the service must normalise it to a list.

    Left as the raw query string, DOT's ``ResourceJSONField`` refuses to store it and the
    whole flow 500s — this is the regression guard for that.
    """
    _, challenge = pkce()
    decide(session_client, public_oauth_app, challenge, "org:read", allow=True)
    assert Grant.objects.get().resource == ["http://testserver"]


def test_access_token_inherits_the_granted_resource(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    run_code_flow(session_client, client, public_oauth_app, "org:read")
    assert AccessToken.objects.get().resource == ["http://testserver"]


def test_deny_redirects_with_access_denied(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    """R-77: pin the target, so a malformed base URI cannot pass on substrings alone."""
    _, challenge = pkce()
    redirect_to = decide(session_client, public_oauth_app, challenge, "org:read", allow=False)
    assert redirect_to.startswith(REDIRECT_URI + "?"), redirect_to
    query = parse_qs(urlparse(redirect_to).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["xyz"]
    assert "code" not in query
    assert not Grant.objects.exists()


def test_decision_without_allow_is_rejected(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    """No code may be issued without an explicit decision in the body."""
    _, challenge = pkce()
    resp = session_client.post(
        authorize_url(public_oauth_app, challenge, "org:read"),
        data={},
        content_type="application/json",
    )
    assert resp.status_code == 422, resp.content
    assert not Grant.objects.exists()


def test_deactivated_app_cannot_refresh_or_userinfo(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    tokens = run_code_flow(session_client, client, public_oauth_app, "openid org:read offline_access")
    public_oauth_app.is_active = False
    public_oauth_app.save(update_fields=["is_active"])
    assert client.get("/o/userinfo", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").status_code == 401
    assert client.post(
        "/o/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": public_oauth_app.client_id,
        },
    ).status_code in (400, 401)


def test_prompt_none_without_grant_is_interaction_required(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    _, challenge = pkce()
    resp = session_client.get(
        "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "openid org:read", prompt="none")
    )
    assert resp.status_code == 200, resp.content
    assert "error=interaction_required" in resp.json()["redirect_to"]
    assert not Grant.objects.exists()


def test_prior_grant_auto_approves(session_client: Client, client: Client, public_oauth_app: OAuthApplication) -> None:
    run_code_flow(session_client, client, public_oauth_app, "org:read offline_access")
    _, challenge = pkce()
    resp = session_client.get("/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read"))
    assert resp.status_code == 200, resp.content
    assert "code=" in resp.json()["redirect_to"]


def test_prompt_none_with_prior_grant_issues_a_code(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """``prompt=none`` is satisfiable without interaction once a grant exists."""
    run_code_flow(session_client, client, public_oauth_app, "openid org:read offline_access")
    _, challenge = pkce()
    resp = session_client.get(
        "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "openid org:read", prompt="none")
    )
    assert resp.status_code == 200, resp.content
    assert "code=" in resp.json()["redirect_to"]


def test_prior_grant_does_not_auto_approve_an_unrestricted_request(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """R-75: an audience-bound grant must not silently mint an unrestricted token."""
    run_code_flow(session_client, client, public_oauth_app, "org:read offline_access")
    _, challenge = pkce()
    data = describe_consent(session_client, public_oauth_app, challenge, "org:read", resource="")
    assert "redirect_to" not in data, data
    assert [s["name"] for s in data["scopes"]] == ["org:read"]


def test_unrestricted_prior_grant_auto_approves_a_narrower_request(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """The converse of R-75: no resource is the universal set, so it covers any audience."""
    run_code_flow(session_client, client, public_oauth_app, "org:read offline_access", resource="")
    _, challenge = pkce()
    data = describe_consent(session_client, public_oauth_app, challenge, "org:read")
    assert "code=" in data["redirect_to"]


def test_widening_scope_after_a_grant_needs_consent_again(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """A prior grant only covers scopes it actually contains."""
    run_code_flow(session_client, client, public_oauth_app, "org:read offline_access")
    _, challenge = pkce()
    resp = session_client.get(
        "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read org:events")
    )
    assert resp.status_code == 200, resp.content
    assert [s["name"] for s in resp.json()["scopes"]] == ["org:read", "org:events"]
