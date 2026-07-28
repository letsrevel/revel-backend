"""Sale controls for membership subscription plans.

Split out of :mod:`events.service.subscription_service` (file-length cap).
Two independent knobs on ``MembershipSubscriptionPlan``:

- ``sales_status`` — PAUSED stops member self-service sales; staff bypass it.
- ``max_subscriptions`` — hard cap on concurrent non-terminal subscriptions
  (the venue's "card stock"); applies to staff too.
"""

from django.utils.translation import gettext as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import MembershipSubscriptionPlan, Organization, OrganizationMember
from events.service.blacklist_service import check_user_hard_blacklisted


def ensure_plan_on_sale(plan: MembershipSubscriptionPlan) -> None:
    """Refuse member self-service sales on a PAUSED plan.

    Applies to member-facing subscribe / revive / plan switches only — staff
    endpoints skip this check (an organizer who paused public sales can still
    manage subscriptions manually). Existing subscribers are never affected.
    """
    if plan.sales_status == MembershipSubscriptionPlan.SalesStatus.PAUSED:
        raise HttpError(400, str(_("Sales for this plan are currently paused.")))


def ensure_plan_sales_capacity(plan: MembershipSubscriptionPlan) -> None:
    """Enforce the plan's cap on concurrent non-terminal subscriptions.

    The cap is the venue's "card stock": it counts PENDING/ACTIVE/PAUSED/
    PAST_DUE subscriptions — including ones scheduled to downgrade into the
    plan (``pending_plan`` reserves the slot, since the Stripe schedule
    rollover re-points ``plan`` with no capacity re-check) — so a cancelled
    or expired subscription frees its slot automatically; there is no counter
    to drift. When a cap is set the plan row is locked so concurrent
    creations serialize (mirrors the capacity-reclaim invariants of ticket
    tiers); callers must already be inside a transaction. Applies to staff
    creation too: capacity is a hard limit, unlike the sales-status pause.
    """
    if plan.max_subscriptions is None:
        return
    locked_plan = MembershipSubscriptionPlan.objects.select_for_update().get(pk=plan.pk)
    if locked_plan.max_subscriptions is None:  # changed since the unlocked read
        return
    if locked_plan.occupied_slot_count() >= locked_plan.max_subscriptions:
        raise HttpError(400, str(_("This plan is sold out.")))


def ensure_member_not_excluded(user: RevelUser, organization: Organization, *, member_facing: bool) -> None:
    """Refuse a BANNED or hard-blacklisted user with the wording its audience needs.

    ``member_facing`` (the subscriber is the caller) collapses both cases into
    one neutral, first-person sentence: the member-facing subscribe path hides
    a hard blacklist behind a 404 (``Organization.objects.for_user()``), so no
    other member-facing path may confirm one. Staff callers keep the specific
    reason — the admin console is where the distinction is actionable.
    """
    banned = OrganizationMember.objects.filter(
        organization=organization,
        user=user,
        status=OrganizationMember.MembershipStatus.BANNED,
    ).exists()
    if banned:
        if member_facing:
            raise HttpError(403, str(_("You can't rejoin this organization.")))
        raise HttpError(403, str(_("This user is banned from the organization.")))

    if check_user_hard_blacklisted(user, organization):
        if member_facing:
            raise HttpError(403, str(_("You can't rejoin this organization.")))
        raise HttpError(403, str(_("This user is blacklisted from the organization.")))
