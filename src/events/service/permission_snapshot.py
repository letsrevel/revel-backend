"""Cached per-user snapshot of the my-permissions payload (#880).

``GET /permissions/my-permissions`` is the third most-called API view (~147k req/week —
the frontend auth store and five SSR loaders hit it on nearly every navigation), so the
rendered payload is cached per user for a short TTL.

Safety model: no server-side authorization ever reads this payload — every admin/staff/
purchase endpoint re-checks ``has_org_permission``/``is_owner_or_staff``/membership
against the DB per request — so a stale entry can only mis-render UI affordances for up
to the TTL, never grant access. Explicit invalidation therefore exists only where a
*grant* is part of an interactive flow and a stale entry would 403 or blank the very
surface the grant was made for: org creation, invitation-token claim, and staff grants
(add_staff, update_staff_permissions — #884). Every other mutation (staff/member
removals, bans, subscription syncs, admin edits, cascades) deliberately rides the TTL —
narrowing directions can only mis-render dead affordances, never leak access.

Cache ops fail open: a broken/unreachable Redis degrades to the plain DB build and never
fails a request or a mutation.
"""

import typing as t
from uuid import UUID

import structlog
from django.core.cache import cache
from django.db import transaction

from accounts.models import RevelUser
from events import models, schema
from events.models import OrganizationMember

logger = structlog.get_logger(__name__)

# Bump when the payload shape changes (OrganizationPermissionsSchema /
# MinimalOrganizationMemberSchema / MembershipTierSchema) so a rolling deploy never
# serves an old-shape entry to new code.
CACHE_VERSION = "v1"
CACHE_TTL_SECONDS = 60


def get_cache_key(user_id: UUID | str) -> str:
    """Cache key for a user's my-permissions payload."""
    return f"my_permissions:{CACHE_VERSION}:{user_id}"


def get_my_permissions_payload(user: RevelUser) -> dict[str, t.Any]:
    """Return the my-permissions payload for ``user``, cached for CACHE_TTL_SECONDS."""
    key = get_cache_key(user.id)
    try:
        cached = cache.get(key)
    except Exception:
        logger.warning("my_permissions_cache_get_failed", exc_info=True)
        cached = None
    if cached is not None:
        return t.cast(dict[str, t.Any], cached)

    payload = build_my_permissions_payload(user)
    try:
        cache.set(key, payload, timeout=CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("my_permissions_cache_set_failed", exc_info=True)
    return payload


def build_my_permissions_payload(user: RevelUser) -> dict[str, t.Any]:
    """Build the payload from the DB (no caching)."""
    staff_perms: dict[str, t.Any] = {
        str(org_id): perms
        for org_id, perms in models.OrganizationStaff.objects.filter(user=user).values_list(
            "organization_id", "permissions"
        )
    }
    owner_perms = {
        str(org_id): "owner" for org_id in models.Organization.objects.filter(owner=user).values_list("id", flat=True)
    }
    permissions = {**staff_perms, **owner_perms}

    memberships: dict[str, schema.MinimalOrganizationMemberSchema] = {}
    members = (
        models.OrganizationMember.objects.select_related("tier")
        .filter(user=user)
        .exclude(status=OrganizationMember.MembershipStatus.BANNED)
    )
    for member in members:
        memberships[str(member.organization_id)] = schema.MinimalOrganizationMemberSchema.from_orm(member)

    return schema.OrganizationPermissionsSchema(
        organization_permissions=permissions, memberships=memberships
    ).model_dump(mode="json")


def invalidate_my_permissions(*user_ids: UUID | str) -> None:
    """Schedule a post-commit cache delete for the given users' payloads.

    ``on_commit`` (not an in-transaction delete): under ``ATOMIC_REQUESTS`` a delete
    issued before commit lets any concurrent request re-cache the *old* committed
    state for the full TTL. Deleting after commit closes that race; on rollback the
    callback is discarded, which is correct — the cached value is still true.
    """
    keys = [get_cache_key(user_id) for user_id in user_ids]

    def _delete() -> None:
        try:
            cache.delete_many(keys)
        except Exception:
            logger.warning("my_permissions_cache_delete_failed", exc_info=True)

    transaction.on_commit(_delete)
