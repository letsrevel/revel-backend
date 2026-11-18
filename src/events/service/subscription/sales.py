"""Sale controls for membership subscription plans.

Split out of :mod:`events.service.subscription.lifecycle` (file-length cap).
Two independent knobs on ``MembershipSubscriptionPlan``:

- ``sales_status`` — PAUSED stops member self-service sales; staff bypass it.
- ``max_subscriptions`` — hard cap on concurrent non-terminal subscriptions
  (the venue's "card stock"); applies to staff too.
"""

import typing as t

from django.db import connection
from django.utils.translation import gettext as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import MembershipSubscriptionPlan, Organization
from events.service.blacklist_service import check_user_hard_blacklisted
from events.utils.membership_access import banned_memberships


def ensure_plan_on_sale(plan: MembershipSubscriptionPlan) -> None:
    """Refuse member self-service sales on a PAUSED plan.

    Applies to member-facing subscribe / revive / plan switches only — staff
    endpoints skip this check (an organizer who paused public sales can still
    manage subscriptions manually). Existing subscribers are never affected.
    """
    if plan.sales_status == MembershipSubscriptionPlan.SalesStatus.PAUSED:
        raise HttpError(400, str(_("Sales for this plan are currently paused.")))


_LOCK_REFRESH_FIELDS = [
    "currency",
    "price",
    "period_unit",
    "period_count",
    "stripe_price_id",
    "stripe_product_id",
    "is_active",
    "sales_status",
    "max_subscriptions",
    "payment_method",
]  # scalar columns only: cached FK relations (tier, organization) stay loaded


def ensure_plan_sales_capacity(plan: MembershipSubscriptionPlan) -> None:
    """Enforce the plan's cap on concurrent non-terminal subscriptions.

    The cap is the venue's "card stock": it counts PENDING/ACTIVE/PAUSED/
    PAST_DUE subscriptions — including ones scheduled to downgrade into the
    plan (``pending_plan`` reserves the slot, since the Stripe schedule
    rollover re-points ``plan`` with no capacity re-check) — so a cancelled
    or expired subscription frees its slot automatically; there is no counter
    to drift. With a cap, the plan row is locked ``FOR UPDATE`` so concurrent
    creations serialize (mirrors the capacity-reclaim invariants of ticket
    tiers). Without one, it takes ``FOR KEY SHARE``: creations don't block
    each other (no serialization across the Checkout-session Stripe call),
    but ``update_plan``'s ``FOR UPDATE`` waits for in-flight creations and
    they wait for it, so its currency/cadence guard cannot miss one (#1427).
    Django's FKs are ``DEFERRABLE INITIALLY DEFERRED``, so the insert itself
    locks nothing until commit. Callers must already be inside a transaction.
    Once the lock is held the caller's ``plan`` instance is refreshed in place,
    so the rest of the creation uses the committed currency, cadence, price,
    Stripe price and cap even if it waited out a concurrent ``update_plan``.
    The sales-pause and active state are not re-checked here: a checkout that
    already passed the sales checks when a pause commits still completes. An
    uncapped plan that gained a cap while waiting falls through to the capped
    path.
    Applies to staff creation too: capacity is a hard limit, unlike the
    sales-status pause.
    """
    if plan.max_subscriptions is None:
        meta = MembershipSubscriptionPlan._meta
        quote = connection.ops.quote_name
        sql = f"SELECT 1 FROM {quote(meta.db_table)} WHERE {quote(meta.pk.attname)} = %s FOR KEY SHARE"  # noqa: S608  # nosec B608 - identifiers from model meta
        with connection.cursor() as cursor:
            cursor.execute(sql, [plan.pk])
        plan.refresh_from_db(fields=_LOCK_REFRESH_FIELDS)  # we may have waited out an update_plan
        if plan.max_subscriptions is None:
            return
        # A cap was set while we waited: count against the locked row below.
        # ponytail: two creations that both hold KEY SHARE and both upgrade here
        # deadlock; Postgres aborts one (that request errors, the cap holds). Only
        # possible right after an uncapped plan gains a cap mid-checkout; upgrade
        # path if it ever matters: catch the deadlock and retry the lock once.
    plan.refresh_from_db(
        fields=_LOCK_REFRESH_FIELDS, from_queryset=MembershipSubscriptionPlan.objects.select_for_update()
    )
    # refresh_from_db may have changed it after mypy narrowed the attribute above.
    cap = t.cast("int | None", plan.max_subscriptions)
    if cap is None:  # changed since the unlocked read
        return
    if plan.occupied_slot_count() >= cap:
        raise HttpError(400, str(_("This plan is sold out.")))


def ensure_member_not_excluded(user: RevelUser, organization: Organization, *, member_facing: bool) -> None:
    """Refuse a BANNED or hard-blacklisted user with the wording its audience needs.

    ``member_facing`` (the subscriber is the caller) collapses both cases into
    one neutral, first-person sentence: the member-facing subscribe path hides
    a hard blacklist behind a 404 (``Organization.objects.for_user()``), so no
    other member-facing path may confirm one. Staff callers keep the specific
    reason — the admin console is where the distinction is actionable.
    """
    banned = banned_memberships().filter(organization=organization, user=user).exists()
    if banned:
        if member_facing:
            raise HttpError(403, str(_("You can't rejoin this organization.")))
        raise HttpError(403, str(_("This user is banned from the organization.")))

    if check_user_hard_blacklisted(user, organization):
        if member_facing:
            raise HttpError(403, str(_("You can't rejoin this organization.")))
        raise HttpError(403, str(_("This user is blacklisted from the organization.")))
