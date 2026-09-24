"""Tests for the connected-apps surface at ``/api/oauth/connections`` (spec §8.4).

The contract that matters here is R-78: ``authorize_service.has_prior_grant`` auto-approves the
next authorization request when the user holds *either* a live access token *or* an unrevoked
refresh token, so a revoke that misses either one leaves the user believing they disconnected an
app that is in fact still authorized — and silently re-authorized without a consent screen. The
end-to-end test at the bottom of this module is that contract; it drives the real flow rather
than counting rows.
"""

import datetime as dt
import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone
from oauth2_provider.models import AccessToken, RefreshToken

from accounts.models import RevelUser
from oauth.models import OAuthApplication
from oauth.tests.test_auth_class import make_access_token
from oauth.tests.test_flow import describe_consent, pkce, run_code_flow
from oauth.tests.test_token_service import make_token_pair

pytestmark = pytest.mark.django_db


def test_list_and_revoke(session_client: Client, user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, "org:read profile")
    [conn] = session_client.get("/api/oauth/connections/").json()
    assert conn["application"]["name"] == "Test App"
    assert sorted(conn["scopes"]) == ["org:read", "profile"]
    # The revoke URL is built from the listing itself, as the Connected Apps screen must: the
    # listing once omitted ``client_id``, which only a fixture-driven test could not notice.
    assert session_client.delete(f"/api/oauth/connections/{conn['client_id']}").status_code == 204
    assert not AccessToken.objects.filter(user=user, application=oauth_app).exists()
    assert session_client.get("/api/oauth/connections/").json() == []


def test_listing_never_exposes_another_identity(
    session_client: Client, user: RevelUser, revel_user_factory: t.Any, oauth_app: OAuthApplication
) -> None:
    """Spec §8.3 counts connections; no surface names the users behind them."""
    make_access_token(user, oauth_app, "org:read")
    make_access_token(revel_user_factory(), oauth_app, "org:read")
    [conn] = session_client.get("/api/oauth/connections/").json()
    assert set(conn) == {"client_id", "application", "scopes", "first_authorized_at", "last_used_at"}
    assert set(conn["application"]) == {
        "name",
        "description",
        "logo_url",
        "verified",
        "registration_source",
        "homepage_url",
        "privacy_policy_url",
    }


def test_another_users_connection_is_invisible(
    session_client: Client, revel_user_factory: t.Any, oauth_app: OAuthApplication
) -> None:
    make_access_token(revel_user_factory(), oauth_app, "org:read")
    assert session_client.get("/api/oauth/connections/").json() == []


def test_revoke_only_touches_my_own_tokens(
    session_client: Client, user: RevelUser, revel_user_factory: t.Any, oauth_app: OAuthApplication
) -> None:
    other = revel_user_factory()
    make_access_token(user, oauth_app, "org:read")
    make_access_token(other, oauth_app, "org:read")
    assert session_client.delete(f"/api/oauth/connections/{oauth_app.client_id}").status_code == 204
    assert AccessToken.objects.filter(user=other, application=oauth_app).count() == 1


def test_revoke_kills_the_refresh_token_row(
    session_client: Client, user: RevelUser, oauth_app: OAuthApplication
) -> None:
    access, refresh = make_token_pair(user, oauth_app, "org:read offline_access")
    assert session_client.delete(f"/api/oauth/connections/{oauth_app.client_id}").status_code == 204
    assert not AccessToken.objects.filter(pk=access.pk).exists()
    refresh.refresh_from_db()
    assert refresh.revoked is not None


def test_revoke_unknown_client_is_404(session_client: Client) -> None:
    assert session_client.delete("/api/oauth/connections/nope").status_code == 404


def test_unverified_email_can_still_list_and_revoke(
    session_client: Client, user: RevelUser, oauth_app: OAuthApplication
) -> None:
    """R-73: verification gates publishing an app, never seeing or cutting off your own grants."""
    make_access_token(user, oauth_app, "org:read")
    user.email_verified = False
    user.save(update_fields=["email_verified"])
    assert len(session_client.get("/api/oauth/connections/").json()) == 1
    assert session_client.delete(f"/api/oauth/connections/{oauth_app.client_id}").status_code == 204


def test_revoking_a_connection_stops_auto_approval_and_refresh(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """R-78, end to end: a revoked connection is no longer a prior grant, and cannot refresh.

    Both halves of ``has_prior_grant`` are covered: the refresh POST fails only if the refresh
    token died, and the consent screen comes back only if the access token died too — an
    incomplete revoke returns an auto-approval redirect here instead.
    """
    scope = "openid org:read offline_access"
    tokens = run_code_flow(session_client, client, public_oauth_app, scope)
    assert tokens["refresh_token"]
    assert AccessToken.objects.filter(application=public_oauth_app).exists()

    _verifier, challenge = pkce()
    auto_approved = describe_consent(session_client, public_oauth_app, challenge, scope)
    assert "redirect_to" in auto_approved, "a prior grant should auto-approve before revocation"

    assert session_client.delete(f"/api/oauth/connections/{public_oauth_app.client_id}").status_code == 204

    # The consent check comes first deliberately: replaying a revoked refresh token trips DOT's
    # reuse protection, which revokes the whole token family and deletes its access tokens
    # (``RefreshToken.revoke_family``). Probing the refresh endpoint first would therefore
    # repair an incomplete revocation and hide exactly the bug this test exists to catch.
    after = describe_consent(session_client, public_oauth_app, challenge, scope)
    assert "consent_ticket" in after, f"revoked connection was auto-approved again: {after}"
    assert "redirect_to" not in after

    refreshed = client.post(
        "/o/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": public_oauth_app.client_id,
        },
    )
    assert refreshed.status_code == 400, refreshed.content
    assert not RefreshToken.objects.filter(application=public_oauth_app, revoked__isnull=True).exists()


def test_a_refresh_only_connection_is_listed_and_revocable(
    session_client: Client, client: Client, public_oauth_app: OAuthApplication
) -> None:
    """R-83, end to end: an idle ``offline_access`` client is still a connection the user can cut off.

    Expiring the access token is the state a real client reaches within the hour:
    ``ACCESS_TOKEN_EXPIRE_SECONDS`` is 3600 while the refresh token behind it stays good for
    ``REFRESH_TOKEN_EXPIRE_SECONDS`` (30 days), and ``has_prior_grant`` keeps auto-approving on
    it the whole time — so the connection has to remain visible and revocable.
    """
    scope = "openid org:read offline_access"
    tokens = run_code_flow(session_client, client, public_oauth_app, scope)
    AccessToken.objects.filter(application=public_oauth_app).update(expires=timezone.now() - dt.timedelta(hours=1))

    [conn] = session_client.get("/api/oauth/connections/").json()
    assert conn["application"]["name"] == "Public App"
    assert "offline_access" in conn["scopes"]

    assert session_client.delete(f"/api/oauth/connections/{public_oauth_app.client_id}").status_code == 204
    assert session_client.get("/api/oauth/connections/").json() == []
    refreshed = client.post(
        "/o/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": public_oauth_app.client_id,
        },
    )
    assert refreshed.status_code == 400, refreshed.content
