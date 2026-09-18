"""Revocation reach and the two beat sweeps (spec §10).

The lifecycle contract these pin: every path that invalidates a user's sessions must also
reach DOT's tables, and the sweeps must not reap a client that is merely idle.
"""

import datetime as dt
import typing as t

import pytest
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken as SessionRefreshToken
from oauth2_provider.models import AccessToken, Grant, RefreshToken
from oauth2_provider.settings import oauth2_settings
from pytest_django.fixtures import Settings

from accounts.jwt import blacklist_user_tokens
from accounts.models import RevelUser
from accounts.service.global_ban_service import deactivate_user_for_ban
from oauth.models import OAuthApplication
from oauth.tasks import clear_expired_tokens, prune_unused_dynamic_clients
from oauth.tests.test_auth_class import make_access_token
from oauth.tests.test_token_service import make_token_pair

pytestmark = pytest.mark.django_db


def test_blacklist_user_tokens_revokes_app_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """A global ban / email rotation must not leave a live app token behind."""
    make_access_token(user, oauth_app, "org:read")
    blacklist_user_tokens(user)
    assert not AccessToken.objects.filter(user=user).exists()


def test_blacklist_user_tokens_revokes_refresh_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """``has_prior_grant`` honours an unrevoked refresh token, so a partial revoke re-grants."""
    make_token_pair(user, oauth_app)
    blacklist_user_tokens(user)
    assert not RefreshToken.objects.filter(user=user, revoked__isnull=True).exists()


def test_blacklist_user_tokens_returns_session_token_count(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """The documented return value counts session JWTs; OAuth revocations are logged separately.

    One of each, so the answer distinguishes "sessions only" (1) from "everything folded in" (2)
    — asserting 0 against an empty database would also pass for a function returning a constant.
    """
    SessionRefreshToken.for_user(user)
    make_access_token(user, oauth_app, "org:read")
    assert blacklist_user_tokens(user) == 1
    assert not AccessToken.objects.filter(user=user).exists()


def test_blacklist_user_tokens_revokes_even_when_provider_disabled(
    settings: Settings, user: RevelUser, oauth_app: OAuthApplication
) -> None:
    """R-108: the feature flag governs whether tokens can be *issued*, not whether a ban lands.

    Gating the revoke on the flag would make ``ScopedJWTAuth``'s ``user.is_active`` check the
    only guard, and would open a disable → ban → re-enable window in which rows that look
    revoked come back to life.
    """
    make_access_token(user, oauth_app, "org:read")
    settings.OIDC_SIGNING_KEY_PATH = ""
    blacklist_user_tokens(user)
    assert not AccessToken.objects.filter(user=user).exists()


def test_clear_expired_tokens_task(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, "org:read", expires_in=-10)
    clear_expired_tokens()
    assert not AccessToken.objects.exists()


def test_clear_expired_tokens_keeps_live_tokens(user: RevelUser, oauth_app: OAuthApplication) -> None:
    make_access_token(user, oauth_app, "org:read")
    clear_expired_tokens()
    assert AccessToken.objects.count() == 1


def _dynamic(name: str, created_hours_ago: int) -> OAuthApplication:
    """A dynamically registered app aged ``created_hours_ago``, holding only its DCR credential.

    ``allowed_scopes`` is deliberately not passed: ``OAuthApplication.save()`` seeds the whole
    scope vocabulary on insert for a DCR-source app, so any value given here (including ``[]``)
    would be overwritten and the argument would read as a claim the model does not honour.
    """
    app = OAuthApplication.objects.create(
        name=name,
        client_type=OAuthApplication.CLIENT_PUBLIC,
        redirect_uris="https://x.example/cb",
        registration_source=OAuthApplication.RegistrationSource.DCR,
    )
    OAuthApplication.objects.filter(pk=app.pk).update(created=timezone.now() - dt.timedelta(hours=created_hours_ago))
    # Every DCR registration mints exactly this one token, so holding it is not "being used".
    make_access_token(None, app, oauth2_settings.DCR_REGISTRATION_SCOPE)
    return app


def test_prune_ignores_registration_token_but_keeps_used_apps(user: RevelUser) -> None:
    _dynamic("stale", 48)
    fresh = _dynamic("fresh", 1)
    used = _dynamic("used", 48)
    make_access_token(user, used, "org:read")
    prune_unused_dynamic_clients()
    remaining = set(OAuthApplication.objects.values_list("name", flat=True))
    assert "stale" not in remaining
    assert {"fresh", "used"} <= remaining
    # The kept apps' own credentials survived with them.
    assert AccessToken.objects.filter(application__in=[fresh, used]).count() == 3


def test_prune_keeps_an_app_with_a_pending_grant(user: RevelUser) -> None:
    """A grant mid-flight means the client is in use; only the code has not been redeemed yet."""
    pending = _dynamic("pending", 48)
    Grant.objects.create(
        user=user,
        application=pending,
        code="grant-code",
        expires=timezone.now() + dt.timedelta(seconds=60),
        redirect_uri="https://x.example/cb",
        scope="org:read",
    )
    prune_unused_dynamic_clients()
    assert OAuthApplication.objects.filter(pk=pending.pk).exists()


def test_prune_keeps_an_app_that_has_been_used(user: RevelUser) -> None:
    """R-114: "unused" is a fact about the app's history, not about what it holds right now.

    A user who authorizes a dynamic client and then disconnects it — or a grant whose only
    access token ``cleartokens`` has since reaped — leaves the app with no artifact at all.
    Deleting it there would break the ``client_id`` for every *other* user of that client, and
    an MCP host that caches its registration gets ``invalid_client`` instead of re-registering.
    """
    used_once = _dynamic("used-once", 48)
    OAuthApplication.objects.filter(pk=used_once.pk).update(last_used_at=timezone.now() - dt.timedelta(hours=30))
    prune_unused_dynamic_clients()
    assert OAuthApplication.objects.filter(pk=used_once.pk).exists()


def test_ban_deactivates_and_revokes_apps_owned_by_the_banned_user(
    user: RevelUser, oauth_app: OAuthApplication, revel_user_factory: t.Any
) -> None:
    """R-116: a ban must end the banned party's access, not just their own sessions.

    ``revoke_user_tokens`` filters on the resource owner, so tokens *other* users granted to a
    banned developer's app would otherwise stay live and the banned owner would keep operating
    that client against those users' data. The blast radius is intended: every user of the app
    loses access, and ``is_active`` is reversible if the ban is lifted.
    """
    granted_by_someone_else = revel_user_factory()
    make_access_token(granted_by_someone_else, oauth_app, "org:read")
    deactivate_user_for_ban(user, "spam")
    oauth_app.refresh_from_db()
    assert oauth_app.is_active is False
    assert not AccessToken.objects.filter(application=oauth_app).exists()


def test_prune_keeps_manual_apps(oauth_app: OAuthApplication) -> None:
    """Only dynamically registered clients are swept; a manual app has an owner to answer for it."""
    OAuthApplication.objects.filter(pk=oauth_app.pk).update(created=timezone.now() - dt.timedelta(hours=48))
    prune_unused_dynamic_clients()
    assert OAuthApplication.objects.filter(pk=oauth_app.pk).exists()


def test_prune_returns_the_number_of_apps_not_cascade_rows() -> None:
    """R-23: two apps carrying two registration tokens is a count of 2, not 4."""
    _dynamic("stale-one", 48)
    _dynamic("stale-two", 48)
    assert prune_unused_dynamic_clients() == 2
    assert not OAuthApplication.objects.exists()
    assert not AccessToken.objects.exists()
