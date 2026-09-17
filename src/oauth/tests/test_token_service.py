"""Tests for ``oauth.service.token_service``: revocation and the connected-apps query.

Revocation is the security-critical half of this module: ``authorize_service.has_prior_grant``
treats *either* a live access token *or* an unrevoked refresh token as a prior grant that
silently auto-approves the next authorization request, so a revoke that misses either one is
revocation that does not revoke (R-78). The end-to-end proof of that contract lives in
``test_connections_controller.py``; the assertions here pin the row-level behaviour it rests on.
"""

import datetime as dt
import typing as t
import uuid

import pytest
from django.utils import timezone
from oauth2_provider.models import AccessToken, Grant, IDToken, RefreshToken, set_token_value
from oauth2_provider.settings import oauth2_settings

from accounts.models import RevelUser
from oauth.models import OAuthApplication
from oauth.service import token_service
from oauth.tests.test_auth_class import make_access_token

pytestmark = pytest.mark.django_db


def make_token_pair(
    user: RevelUser, app: OAuthApplication, scopes: str = "org:read", *, expires_in: int = 3600
) -> tuple[AccessToken, RefreshToken]:
    """Create the access + refresh pair a real ``offline_access`` grant leaves behind.

    Args:
        user: The resource owner.
        app: The client the tokens were issued to.
        scopes: The space-separated scope string stored on the access token.
        expires_in: Seconds until the access token expires; negative for an expired one.

    Returns:
        The saved access token and its refresh token.
    """
    access = AccessToken(
        user=user, application=app, scope=scopes, expires=timezone.now() + dt.timedelta(seconds=expires_in)
    )
    set_token_value(access, uuid.uuid4().hex)
    access.save()
    refresh = RefreshToken(user=user, application=app, access_token=access, token_family=uuid.uuid4())
    set_token_value(refresh, uuid.uuid4().hex)
    refresh.save()
    return access, refresh


def make_grant(user: RevelUser, app: OAuthApplication, scopes: str = "org:read") -> Grant:
    """Create a pending authorization code for ``user`` at ``app``."""
    return Grant.objects.create(
        user=user,
        application=app,
        code=uuid.uuid4().hex,
        expires=timezone.now() + dt.timedelta(seconds=60),
        redirect_uri="https://app.example/cb",
        scope=scopes,
    )


def test_revoke_user_tokens_scoped_to_app(
    user: RevelUser, oauth_app: OAuthApplication, public_oauth_app: OAuthApplication
) -> None:
    make_access_token(user, oauth_app, "org:read")
    make_access_token(user, public_oauth_app, "org:read")
    assert token_service.revoke_user_tokens(user, application=oauth_app) == 1
    assert AccessToken.objects.filter(user=user).count() == 1
    assert AccessToken.objects.filter(user=user, application=public_oauth_app).exists()


def test_revoke_user_tokens_across_every_app(
    user: RevelUser, oauth_app: OAuthApplication, public_oauth_app: OAuthApplication
) -> None:
    make_access_token(user, oauth_app, "org:read")
    make_access_token(user, public_oauth_app, "org:read")
    assert token_service.revoke_user_tokens(user) == 2
    assert not AccessToken.objects.filter(user=user).exists()


def test_revoke_user_tokens_leaves_other_users_alone(
    user: RevelUser, revel_user_factory: t.Any, oauth_app: OAuthApplication
) -> None:
    other = revel_user_factory()
    make_access_token(user, oauth_app, "org:read")
    make_access_token(other, oauth_app, "org:read")
    assert token_service.revoke_user_tokens(user, application=oauth_app) == 1
    assert AccessToken.objects.filter(user=other, application=oauth_app).count() == 1


def test_revoke_app_tokens(user: RevelUser, revel_user_factory: t.Any, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, "org:read")
    make_access_token(revel_user_factory(), oauth_app, "org:read")
    assert token_service.revoke_app_tokens(oauth_app) == 2
    assert not AccessToken.objects.filter(application=oauth_app).exists()


def test_revoke_app_tokens_ignores_other_apps(
    user: RevelUser, oauth_app: OAuthApplication, public_oauth_app: OAuthApplication
) -> None:
    make_access_token(user, oauth_app, "org:read")
    make_access_token(user, public_oauth_app, "org:read")
    assert token_service.revoke_app_tokens(oauth_app) == 1
    assert AccessToken.objects.filter(application=public_oauth_app).count() == 1


def test_revoke_kills_access_and_refresh_together(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """R-78: a prior grant is a live access token OR an unrevoked refresh token — kill both."""
    access, refresh = make_token_pair(user, oauth_app, "org:read offline_access")
    assert token_service.revoke_user_tokens(user, application=oauth_app) == 2
    assert not AccessToken.objects.filter(pk=access.pk).exists()
    refresh.refresh_from_db()
    assert refresh.revoked is not None
    assert refresh.access_token_id is None


def test_revoke_deletes_pending_grants_and_id_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_grant(user, oauth_app)
    id_token = IDToken.objects.create(
        user=user,
        application=oauth_app,
        scope="openid",
        expires=timezone.now() + dt.timedelta(hours=1),
    )
    token_service.revoke_user_tokens(user, application=oauth_app)
    assert not Grant.objects.filter(user=user, application=oauth_app).exists()
    assert not IDToken.objects.filter(pk=id_token.pk).exists()


def test_revoke_twice_is_a_no_op(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """Revoking again must neither raise nor re-count already-dead credentials."""
    make_token_pair(user, oauth_app, "org:read offline_access")
    assert token_service.revoke_user_tokens(user, application=oauth_app) == 2
    assert token_service.revoke_user_tokens(user, application=oauth_app) == 0


def test_revoke_scoped_tokens_only_touches_tokens_holding_a_removed_scope(
    user: RevelUser, revel_user_factory: t.Any, oauth_app: OAuthApplication
) -> None:
    wide, wide_refresh = make_token_pair(user, oauth_app, "openid org:read")
    narrow, narrow_refresh = make_token_pair(revel_user_factory(), oauth_app, "openid")
    doomed_grant = make_grant(user, oauth_app, "openid org:read")
    safe_grant = make_grant(user, oauth_app, "openid")

    assert token_service.revoke_scoped_tokens(oauth_app, {"org:read"}) == 2

    assert not AccessToken.objects.filter(pk=wide.pk).exists()
    wide_refresh.refresh_from_db()
    assert wide_refresh.revoked is not None
    assert AccessToken.objects.filter(pk=narrow.pk).exists()
    narrow_refresh.refresh_from_db()
    assert narrow_refresh.revoked is None
    assert not Grant.objects.filter(pk=doomed_grant.pk).exists()
    assert Grant.objects.filter(pk=safe_grant.pk).exists()


def test_connections_for(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, "org:read profile")
    [conn] = token_service.connections_for(user)
    assert conn.application == oauth_app
    assert conn.scopes == {"org:read", "profile"}
    assert conn.first_authorized_at <= conn.last_used_at


def test_connections_for_unions_scopes_across_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, "org:read")
    make_access_token(user, oauth_app, "profile email")
    [conn] = token_service.connections_for(user)
    assert conn.scopes == {"org:read", "profile", "email"}


def test_connections_for_skips_expired_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """R-28 / spec §8.4: a dead token is not a connection."""
    make_access_token(user, oauth_app, "org:read", expires_in=-1)
    assert token_service.connections_for(user) == []


def test_connections_for_includes_a_refresh_only_grant(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """R-83 / spec §8.4: a live access token OR an unrevoked refresh token is a connection.

    An ``offline_access`` client spends most of its life here — access token expired, refresh
    token good for 30 days — and ``has_prior_grant`` still auto-approves it, so the list that
    feeds revocation must show it.
    """
    make_token_pair(user, oauth_app, "org:read offline_access", expires_in=-1)
    [conn] = token_service.connections_for(user)
    assert conn.application == oauth_app
    assert conn.scopes == {"org:read", "offline_access"}


def test_connections_for_skips_revoked_refresh_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    _access, refresh = make_token_pair(user, oauth_app, "org:read offline_access", expires_in=-1)
    refresh.revoked = timezone.now()
    refresh.save(update_fields=["revoked"])
    assert token_service.connections_for(user) == []


def test_connections_for_skips_orphaned_refresh_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """A refresh token with no access token is already unusable — DOT rejects it — so it is dead."""
    access, _refresh = make_token_pair(user, oauth_app, "org:read offline_access", expires_in=-1)
    access.delete()
    assert token_service.connections_for(user) == []


def test_connections_for_does_not_double_count_a_live_pair(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_token_pair(user, oauth_app, "org:read offline_access")
    assert len(token_service.connections_for(user)) == 1


def test_connections_for_skips_other_users(
    user: RevelUser, revel_user_factory: t.Any, oauth_app: OAuthApplication
) -> None:
    make_access_token(revel_user_factory(), oauth_app, "org:read")
    assert token_service.connections_for(user) == []


def test_connections_for_skips_dcr_registration_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, oauth2_settings.DCR_REGISTRATION_SCOPE)
    assert token_service.connections_for(user) == []


def test_connections_for_orders_by_last_used_first(
    user: RevelUser, oauth_app: OAuthApplication, public_oauth_app: OAuthApplication
) -> None:
    make_access_token(user, oauth_app, "org:read")
    make_access_token(user, public_oauth_app, "org:read")
    assert [c.application for c in token_service.connections_for(user)] == [public_oauth_app, oauth_app]


def test_connections_for_query_count_is_flat(
    user: RevelUser, oauth_app: OAuthApplication, public_oauth_app: OAuthApplication, django_assert_num_queries: t.Any
) -> None:
    """R-28: no query per application — one scan per token table plus one ``in_bulk``."""
    third = OAuthApplication(
        user=user,
        name="Third App",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        redirect_uris="https://third.example/cb",
        allowed_scopes=["org:read"],
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    third.full_clean()
    third.save()
    for app in (oauth_app, public_oauth_app, third):
        make_access_token(user, app, "org:read")
    with django_assert_num_queries(3):
        assert len(token_service.connections_for(user)) == 3
