"""Unit-level coverage of the headless consent endpoints (no token endpoint involved)."""

import typing as t

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken
from oauth2_provider.models import Grant

from accounts.models import RevelUser
from oauth.models import OAuthApplication
from oauth.scopes import SCOPES
from oauth.service import authorize_service
from oauth.tests.test_flow import authorize_query, authorize_url, decide, describe_consent, pkce

pytestmark = pytest.mark.django_db


def test_describe_lists_app_and_scopes(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    _, challenge = pkce()
    data = session_client.get(
        "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "openid org:read")
    ).json()
    assert data["application"]["name"] == "Public App" and data["application"]["verified"] is False
    assert data["application"]["registration_source"] == "manual"
    assert data["application"]["logo_url"] is None
    assert [s["name"] for s in data["scopes"]] == ["openid", "org:read"]
    assert data["scopes"][1]["group"] == "org" and data["scopes"][1]["label"]
    assert data["scopes"][0]["group"] == "identity"
    assert data["redirect_uri"] == "https://app.example/cb" and data["state"] == "xyz"
    assert data["consent_ticket"]


def test_describe_renders_an_absolute_signed_logo_url(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """Thumbnails live under ``protected/``; a root-relative ``.url`` is unfetchable (R-64)."""
    public_oauth_app.logo_thumbnail = "protected/oauth-logos/logo_thumbnail.jpg"
    public_oauth_app.save(update_fields=["logo_thumbnail"])
    _, challenge = pkce()
    data = session_client.get("/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read")).json()
    logo_url = data["application"]["logo_url"]
    assert logo_url.startswith("http://testserver/") and "sig=" in logo_url


def _post_decision(
    session_client: Client, app: OAuthApplication, challenge: str, scope: str, body: dict[str, t.Any]
) -> t.Any:
    """POST a decision body verbatim, without the helper's ticket plumbing."""
    return session_client.post(authorize_url(app, challenge, scope), data=body, content_type="application/json")


def test_approval_without_a_consent_ticket_is_refused(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """R-74: the backend must hold its own proof that the user saw these scopes."""
    _, challenge = pkce()
    resp = _post_decision(session_client, public_oauth_app, challenge, "org:read", {"allow": True})
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "consent_required"
    assert not Grant.objects.exists()


def test_tampered_consent_ticket_is_refused(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    _, challenge = pkce()
    ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
    forged = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
    resp = _post_decision(
        session_client, public_oauth_app, challenge, "org:read", {"allow": True, "consent_ticket": forged}
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "invalid_request"
    assert not Grant.objects.exists()


def test_expired_consent_ticket_is_refused_distinguishably(
    monkeypatch: pytest.MonkeyPatch, session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """An expired screen asks for ``consent_required`` (re-show it), not ``invalid_request``."""
    _, challenge = pkce()
    ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
    monkeypatch.setattr(authorize_service, "CONSENT_TICKET_TTL_SECONDS", -1)
    resp = _post_decision(
        session_client, public_oauth_app, challenge, "org:read", {"allow": True, "consent_ticket": ticket}
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "consent_required"
    assert not Grant.objects.exists()


def test_widening_scope_between_screen_and_decision_is_refused(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """The point of the ticket: a ticket for narrower scopes cannot approve wider ones."""
    _, challenge = pkce()
    ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
    resp = _post_decision(
        session_client,
        public_oauth_app,
        challenge,
        "org:read org:events",
        {"allow": True, "consent_ticket": ticket},
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "invalid_request"
    assert not Grant.objects.exists()


def test_widening_the_resource_between_screen_and_decision_is_refused(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """The ticket binds the audience too, not just the scopes."""
    _, challenge = pkce()
    ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
    resp = session_client.post(
        authorize_url(public_oauth_app, challenge, "org:read", resource=""),
        data={"allow": True, "consent_ticket": ticket},
        content_type="application/json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "invalid_request"
    assert not Grant.objects.exists()


def test_refusal_needs_no_consent_ticket(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    """A refusal issues no code, so a screen that has since expired can still be answered."""
    _, challenge = pkce()
    resp = _post_decision(session_client, public_oauth_app, challenge, "org:read", {"allow": False})
    assert resp.status_code == 200, resp.content
    assert "error=access_denied" in resp.json()["redirect_to"]
    assert not Grant.objects.exists()


def test_consent_ticket_is_bound_to_one_user(
    session_client: Client, public_oauth_app: OAuthApplication, revel_user_factory: t.Any
) -> None:
    """Another signed-in user cannot spend a ticket minted for someone else."""
    _, challenge = pkce()
    ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
    other = revel_user_factory(email="other@example.com")
    other.email_verified = True
    other.save(update_fields=["email_verified"])
    refresh = RefreshToken.for_user(other)
    other_client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
    resp = other_client.post(
        authorize_url(public_oauth_app, challenge, "org:read"),
        data={"allow": True, "consent_ticket": ticket},
        content_type="application/json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.json()["error"] == "invalid_request"
    assert not Grant.objects.exists()


def test_scope_outside_allowed_is_invalid_scope(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    public_oauth_app.allowed_scopes = ["openid"]
    public_oauth_app.save(update_fields=["allowed_scopes"])
    _, challenge = pkce()
    resp = session_client.get("/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "openid org:read"))
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_scope"


def test_missing_pkce_is_rejected(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    q = authorize_query(public_oauth_app, "x", "org:read")
    del q["code_challenge"], q["code_challenge_method"]
    resp = session_client.get("/api/oauth/authorize", q)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_request"
    assert resp.json()["detail"]


def test_invalid_resource_indicator_is_invalid_target(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    _, challenge = pkce()
    resp = session_client.get(
        "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read", resource="/not-absolute")
    )
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_target"


def test_no_resource_indicator_leaves_the_grant_unrestricted(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """A plain OAuth client sends no ``resource``; the grant must then carry no audience."""
    _, challenge = pkce()
    decide(session_client, public_oauth_app, challenge, "org:read", allow=True, resource="")
    assert not Grant.objects.get().resource


def test_error_redirect_omits_an_absent_state(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    """``state`` is optional (RFC 6749 §4.1.1) and must not be echoed back empty."""
    _, challenge = pkce()
    query = authorize_query(public_oauth_app, challenge, "org:read", prompt="none")
    del query["state"]
    redirect_to = session_client.get("/api/oauth/authorize", query).json()["redirect_to"]
    assert redirect_to == "https://app.example/cb?error=interaction_required"


def test_skip_authorization_auto_approves(session_client: Client, public_oauth_app: OAuthApplication) -> None:
    public_oauth_app.skip_authorization = True
    public_oauth_app.save(update_fields=["skip_authorization"])
    _, challenge = pkce()
    resp = session_client.get("/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read"))
    assert resp.status_code == 200
    assert "code=" in resp.json()["redirect_to"]


def test_prompt_consent_overrides_skip_authorization(
    session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    """``prompt=consent`` (OIDC Core §3.1.2.1) must force the screen even for a trusted app."""
    public_oauth_app.skip_authorization = True
    public_oauth_app.save(update_fields=["skip_authorization"])
    _, challenge = pkce()
    resp = session_client.get(
        "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read", prompt="consent")
    )
    assert resp.status_code == 200
    assert resp.json()["application"]["name"] == "Public App"


def test_error_redirect_preserves_the_registered_query_string(session_client: Client, user: RevelUser) -> None:
    """R-67: ``parse_qsl``/``urlencode``, so an encoded value is not encoded twice."""
    app = OAuthApplication(
        user=user,
        name="Query App",
        client_type=OAuthApplication.CLIENT_PUBLIC,
        client_secret="",
        redirect_uris="https://app.example/cb?next=%2Fa%3Db%26c",
        allowed_scopes=sorted(SCOPES),
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    app.full_clean()
    app.save()
    _, challenge = pkce()
    query = authorize_query(app, challenge, "org:read", prompt="none")
    query["redirect_uri"] = "https://app.example/cb?next=%2Fa%3Db%26c"
    resp = session_client.get("/api/oauth/authorize", query)
    assert resp.status_code == 200, resp.content
    redirect_to = resp.json()["redirect_to"]
    assert "next=%2Fa%3Db%26c" in redirect_to
    assert "error=interaction_required" in redirect_to and "state=xyz" in redirect_to


def test_anonymous_is_401(client: Client, public_oauth_app: OAuthApplication) -> None:
    _, challenge = pkce()
    assert (
        client.get("/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read")).status_code == 401
    )


def test_anonymous_cannot_decide(client: Client, public_oauth_app: OAuthApplication) -> None:
    _, challenge = pkce()
    resp = client.post(
        authorize_url(public_oauth_app, challenge, "org:read"), data={"allow": True}, content_type="application/json"
    )
    assert resp.status_code == 401


def test_disabled_is_404(settings: t.Any, session_client: Client, public_oauth_app: OAuthApplication) -> None:
    settings.OIDC_SIGNING_KEY_PATH = ""
    _, challenge = pkce()
    assert (
        session_client.get("/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "org:read")).status_code
        == 404
    )


def test_disabled_rejects_a_decision(
    settings: t.Any, session_client: Client, public_oauth_app: OAuthApplication
) -> None:
    settings.OIDC_SIGNING_KEY_PATH = ""
    _, challenge = pkce()
    resp = session_client.post(
        authorize_url(public_oauth_app, challenge, "org:read"), data={"allow": True}, content_type="application/json"
    )
    assert resp.status_code == 404
