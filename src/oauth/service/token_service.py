"""Revocation and the "connected apps" query over DOT's token tables (spec §9).

Revocation here has to be *complete*, not merely plausible: ``authorize_service.has_prior_grant``
treats a live access token **or** an unrevoked refresh token as a prior grant and auto-approves
the next authorization request without showing a consent screen (R-78). A revoke that misses
either row leaves the user believing they disconnected an app that is, in fact, re-authorized
silently on its next visit.

Set-based, not row by row. ``RefreshToken.revoke()`` takes a locking ``SELECT`` per row and
deletes its own access token as a side effect, so iterating both tables walks the same rows
twice; ``RefreshToken.revoke_family()`` is DOT's constant-query answer to that but keys on
``token_family``, which skips a NULL family and never touches an access token that has no
refresh token at all (a client that did not ask for ``offline_access``). Filtering on
``(user, application)`` instead is a superset of every family, needs no family lookup, and is
four statements no matter how many tokens the pair has accumulated.
"""

import typing as t
import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from oauth2_provider.models import AccessToken, Grant, IDToken, RefreshToken
from oauth2_provider.settings import oauth2_settings

from accounts.models import RevelUser
from oauth.models import OAuthApplication


@dataclass(frozen=True)
class Connection:
    """One app a user has authorized, folded over every live token it holds."""

    application: OAuthApplication
    scopes: frozenset[str]
    first_authorized_at: datetime
    last_used_at: datetime


@transaction.atomic
def _revoke(
    *,
    access: QuerySet[AccessToken],
    refresh: QuerySet[RefreshToken],
    grants: QuerySet[Grant],
    id_tokens: QuerySet[IDToken],
) -> int:
    """Kill a matched set of credentials and return how many were live.

    Order is load-bearing. The refresh tokens are marked revoked *first*, while they still
    point at their access tokens: deleting the access tokens flips ``RefreshToken.access_token``
    to NULL (it is ``SET_NULL``), and a refresh row that is orphaned but not revoked still reads
    as unrevoked to every audit surface. ``update()`` names ``updated`` explicitly because
    ``auto_now`` does not fire on a queryset update.

    ID tokens are deleted for tidiness, not safety: an ID token is an assertion the client
    already holds as a JWT and is not a credential for this API, so deleting the row cannot
    un-issue it — it only stops a disconnected app's identity data lingering until the next
    ``cleartokens`` sweep. The delete runs after the access tokens because ``AccessToken.id_token``
    cascades, so an ID token deleted first would take its access token with it.

    Args:
        access: Access tokens to delete (DOT's ``revoke()`` is a delete).
        refresh: Refresh tokens to revoke; already-revoked rows are skipped.
        grants: Pending authorization codes to delete.
        id_tokens: ID tokens to delete.

    Returns:
        The number of live credentials invalidated: access tokens deleted plus refresh tokens
        newly revoked. Grants and ID tokens are not credentials and are not counted.
    """
    now = timezone.now()
    revoked = refresh.filter(revoked__isnull=True).update(revoked=now, updated=now)
    _total, per_model = access.delete()
    deleted = per_model.get(AccessToken._meta.label, 0)
    id_tokens.delete()
    grants.delete()
    return revoked + deleted


def _revoke_where(**flt: t.Any) -> int:
    """Revoke every credential matching one ``(user, application)``-shaped filter."""
    return _revoke(
        access=AccessToken.objects.filter(**flt),
        refresh=RefreshToken.objects.filter(**flt),
        grants=Grant.objects.filter(**flt),
        id_tokens=IDToken.objects.filter(**flt),
    )


def revoke_user_tokens(user: RevelUser, application: OAuthApplication | None = None) -> int:
    """Revoke every credential ``user`` granted, optionally narrowed to one app.

    Args:
        user: The resource owner disconnecting an app.
        application: The client to disconnect, or None for every client.

    Returns:
        The number of live credentials invalidated.
    """
    if application is None:
        return _revoke_where(user=user)
    return _revoke_where(user=user, application=application)


def revoke_app_tokens(application: OAuthApplication) -> int:
    """Revoke every credential ``application`` holds, for every user.

    Args:
        application: The client being switched off.

    Returns:
        The number of live credentials invalidated.
    """
    return _revoke_where(application=application)


def _pks_holding_any(queryset: QuerySet[t.Any], scopes: set[str]) -> list[int]:
    """Primary keys of rows whose space-separated ``scope`` column holds one of ``scopes``.

    The test is done in Python rather than SQL because scope names are prefixes of one another
    (``org:read`` inside ``org:read-write``) and every ``LIKE`` spelling of "holds this scope"
    either matches too much or misses the first and last entry.

    Args:
        queryset: Any token/grant queryset already narrowed to one application.
        scopes: The scope names being revoked.

    Returns:
        The matching primary keys.
    """
    return [pk for pk, scope in queryset.values_list("pk", "scope") if scopes & set(scope.split())]


def revoke_scoped_tokens(application: OAuthApplication, scopes: set[str]) -> int:
    """Revoke only what carries one of ``scopes`` — the scope-shrink path (spec §8.3).

    A token that holds none of the removed scopes is untouched: narrowing an app's
    ``allowed_scopes`` withdraws that authority, it does not disconnect every user.

    One knock-on effect, deliberately accepted: ``AccessToken.id_token`` cascades, so deleting an
    ID token whose ``scope`` holds a removed scope also deletes its paired access token even when
    that token's own scope string does not — over-revocation, not under-revocation. It leaves an
    orphaned refresh row, which both ``connections_for`` and ``has_prior_grant`` ignore and DOT's
    ``validate_refresh_token`` refuses, so it fails in the only direction that is safe: the user
    re-consents, and nothing keeps authority it should have lost.

    Args:
        application: The app whose scopes were narrowed.
        scopes: The scopes that were removed.

    Returns:
        The number of live credentials invalidated.
    """
    access_pks = _pks_holding_any(AccessToken.objects.filter(application=application), scopes)
    return _revoke(
        access=AccessToken.objects.filter(pk__in=access_pks),
        # A refresh token stores no scope of its own; its authority is the access token it is
        # paired with. An orphan (NULL access token) is already unusable — DOT's
        # ``validate_refresh_token`` rejects it — so the pairing is the whole live set.
        refresh=RefreshToken.objects.filter(application=application, access_token_id__in=access_pks),
        grants=Grant.objects.filter(pk__in=_pks_holding_any(Grant.objects.filter(application=application), scopes)),
        id_tokens=IDToken.objects.filter(
            pk__in=_pks_holding_any(IDToken.objects.filter(application=application), scopes)
        ),
    )


def connections_for(user: RevelUser) -> list[Connection]:
    """The apps holding live authority for ``user``, most recently used first (spec §8.4).

    A connection is a live access token **OR** an unrevoked refresh token (R-83), which is
    exactly what ``authorize_service.has_prior_grant`` treats as a prior grant. The two must
    agree: with a one-hour access token and a thirty-day refresh token, an idle
    ``offline_access`` client spends most of its life with no live access token, and listing only
    those would hide a grant that auto-approval still honours — leaving the user unable to revoke
    what they cannot see. A refresh token with no access token is excluded: DOT's
    ``validate_refresh_token`` rejects that orphan, so it is already dead.

    Three queries regardless of how many apps the user has authorized (R-28): one pass per token
    table, folded in Python, then one ``in_bulk`` for the applications.

    ``first_authorized_at`` is the oldest *surviving* credential, not necessarily the original
    grant: rotation replaces both tokens on every refresh, so the trail of the first authorization
    is gone once the access token it minted has been superseded.

    Args:
        user: The resource owner.

    Returns:
        One ``Connection`` per app, newest use first.
    """
    live_access = (
        AccessToken.objects.filter(user=user, application__isnull=False, expires__gt=timezone.now())
        # A dynamic client's registration credential is not a user grant. It is issued with no
        # user today (``oauth.views`` forces an anonymous registrant), so this is defence for
        # the deployment that swaps in ``IsAuthenticatedDCRPermission``, exactly as
        # ``common.authentication`` guards the same scope on the token path.
        .exclude(scope__contains=oauth2_settings.DCR_REGISTRATION_SCOPE)
        .values_list("application", "scope", "created", "updated")
    )
    # The scope comes off the paired access token because a refresh token stores none of its own.
    # Idle expiry (``REFRESH_TOKEN_EXPIRE_SECONDS`` past the access token's expiry) is deliberately
    # NOT applied: ``has_prior_grant`` does not apply it either, so a token that old still
    # auto-approves, and hiding it here would reopen the gap this function exists to close.
    live_refresh = (
        RefreshToken.objects.filter(user=user, revoked__isnull=True, access_token__isnull=False)
        # Symmetrical with the access pass above, and for the same reason: a registration
        # credential is not a user grant. Unreachable today twice over (a registration token has
        # no refresh token, and the RFC 7592 path cannot give it a user), but an exclusion that
        # only guards one of two passes is not the defence the comment above claims it is.
        .exclude(access_token__scope__contains=oauth2_settings.DCR_REGISTRATION_SCOPE)
        .values_list("application", "access_token__scope", "created", "updated")
    )
    folded: dict[uuid.UUID, tuple[set[str], datetime, datetime]] = {}
    for app_id, scope, created, updated in [*live_access, *live_refresh]:
        held, first, last = folded.get(app_id, (set(), created, updated))
        held.update(scope.split())
        folded[app_id] = (held, min(first, created), max(last, updated))
    apps = OAuthApplication.objects.in_bulk(list(folded))
    connections = [
        Connection(
            application=apps[app_id],
            scopes=frozenset(held),
            first_authorized_at=first,
            last_used_at=last,
        )
        for app_id, (held, first, last) in folded.items()
    ]
    return sorted(connections, key=lambda connection: connection.last_used_at, reverse=True)
