"""Named predicates for "is this user a member of this organization".

Single source of truth for membership-status semantics, so every read goes through the
same names instead of hand-rolled ``status`` filters:

- *visible* (ACTIVE, PAUSED): may SEE member-only content.
- *eligible* (ACTIVE): may ACT as a member (buy member tiers, exemptions, member comms).
- *banned*: shut out entirely.

Side-effect free; models are imported lazily so it can be imported from model modules
(``controllers → services → models/utils``). Builders return lazy querysets, safe to use
inside ``Exists(... OuterRef(...))``.
"""

import typing as t

if t.TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet

    from accounts.models import RevelUser
    from events.models import OrganizationMember
    from events.models.membership import OrganizationMemberQuerySet

# Plain strings (== OrganizationMember.MembershipStatus values) because models cannot be
# imported here at module level; tests pin them to the enum.
VISIBLE_STATUSES: frozenset[str] = frozenset({"active", "paused"})
ELIGIBLE_STATUSES: frozenset[str] = frozenset({"active"})
BANNED_STATUS: str = "banned"
AWAITING_PAYMENT_STATUS: str = "awaiting_payment"
# Statuses a first offline payment can activate / that an unpaid offline subscription may be holding.
PAYMENT_HELD_STATUSES: frozenset[str] = frozenset({AWAITING_PAYMENT_STATUS, "active"})


def is_visible_status(status: str | None) -> bool:
    """Whether a membership with this status may see member content."""
    return status in VISIBLE_STATUSES


def is_eligible_status(status: str | None) -> bool:
    """Whether a membership with this status may act as a member."""
    return status in ELIGIBLE_STATUSES


def is_awaiting_payment_status(status: str | None) -> bool:
    """Whether a membership with this status is held until its first offline payment."""
    return status == AWAITING_PAYMENT_STATUS


def is_banned_status(status: str | None) -> bool:
    """Whether a membership with this status is banned."""
    return status == BANNED_STATUS


def is_cancelled_status(status: str | None) -> bool:
    """Whether a membership with this status is cancelled."""
    return status == "cancelled"


def is_payment_held_status(status: str | None) -> bool:
    """Whether a first offline payment acts on a membership with this status (AWAITING_PAYMENT, ACTIVE)."""
    return status in PAYMENT_HELD_STATUSES


def visible_memberships() -> "OrganizationMemberQuerySet":
    """Memberships that may see member content (ACTIVE, PAUSED)."""
    from events.models import OrganizationMember

    return OrganizationMember.objects.for_visibility()


def eligible_memberships() -> "OrganizationMemberQuerySet":
    """Memberships that may act as a member (ACTIVE)."""
    from events.models import OrganizationMember

    return OrganizationMember.objects.active_only()


def payment_held_memberships() -> "OrganizationMemberQuerySet":
    """Memberships a first offline payment acts on (AWAITING_PAYMENT, ACTIVE)."""
    from events.models import OrganizationMember

    return OrganizationMember.objects.payment_held()


def awaiting_payment_memberships() -> "OrganizationMemberQuerySet":
    """Memberships held until their first offline payment (AWAITING_PAYMENT)."""
    from events.models import OrganizationMember

    return OrganizationMember.objects.awaiting_payment()


def banned_memberships() -> "OrganizationMemberQuerySet":
    """BANNED memberships."""
    from events.models import OrganizationMember

    return OrganizationMember.objects.banned_only()


def visible_member_org_ids(user: "RevelUser | UUID") -> "QuerySet[OrganizationMember, UUID]":
    """Organizations where the user (instance or raw user id) may see member content."""
    return visible_memberships().filter(user=user).values_list("organization_id", flat=True)


def eligible_member_org_ids(user: "RevelUser | UUID") -> "QuerySet[OrganizationMember, UUID]":
    """Organizations where the user may act as a member."""
    return eligible_memberships().filter(user=user).values_list("organization_id", flat=True)


def banned_org_ids(user: "RevelUser | UUID") -> "QuerySet[OrganizationMember, UUID]":
    """Organizations the user is banned from."""
    return banned_memberships().filter(user=user).values_list("organization_id", flat=True)


def visible_member_user_ids(organization_id: "UUID") -> "QuerySet[OrganizationMember, UUID]":
    """Users who may see member content in the organization."""
    return visible_memberships().filter(organization_id=organization_id).values_list("user_id", flat=True)


def eligible_member_user_ids(
    organization_id: "UUID", tier_ids: "t.Collection[UUID] | None" = None
) -> "QuerySet[OrganizationMember, UUID]":
    """Users who may act as members of the organization, optionally restricted to tiers."""
    qs = eligible_memberships().filter(organization_id=organization_id)
    if tier_ids is not None:
        qs = qs.filter(tier_id__in=tier_ids)
    return qs.values_list("user_id", flat=True)


def is_eligible_member(user: "RevelUser | UUID", organization_id: "UUID") -> bool:
    """Whether the user may act as a member of the organization."""
    return eligible_memberships().filter(user=user, organization_id=organization_id).exists()


def get_eligible_membership(user: "RevelUser | UUID", organization_id: "UUID") -> "OrganizationMember | None":
    """The user's eligible membership in the organization (with ``tier``), if any."""
    return eligible_memberships().select_related("tier").filter(user=user, organization_id=organization_id).first()
