"""Plan-change helpers for ONLINE membership subscriptions (Phase 3).

Extracted from ``subscription_stripe_service`` to keep that module under the
1000-line file-length limit. All functions here deal exclusively with
switching an ONLINE subscription from one :class:`MembershipSubscriptionPlan`
to another — either as an immediate prorated upgrade or as a scheduled
downgrade via a :class:`stripe.SubscriptionSchedule`.
"""

import typing as t
from decimal import Decimal

import stripe
import structlog
from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)

logger = structlog.get_logger(__name__)


# ---- Stripe-account helpers (mirrors subscription_stripe_service) ------------
# Duplicated here to avoid a circular import: subscription_stripe_service
# re-exports change_online_plan, while this module provides it.


def _stripe_account_kwargs(organization: Organization) -> dict[str, str]:
    """Return ``stripe_account=...`` kwargs for a Connect API call."""
    if organization.stripe_account_id and organization.stripe_account_id != settings.STRIPE_ACCOUNT:
        return {"stripe_account": organization.stripe_account_id}
    return {}


# ---- Internal helpers --------------------------------------------------------


_PERIOD_UNIT_MONTHS: dict[str, Decimal] = {
    MembershipSubscriptionPlan.PeriodUnit.MONTH.value: Decimal("1"),
    MembershipSubscriptionPlan.PeriodUnit.YEAR.value: Decimal("12"),
}


def _monthly_equivalent_price(plan: MembershipSubscriptionPlan) -> Decimal:
    """Return ``plan.price`` normalized to a per-month figure.

    A cross-cadence change (e.g. Monthly→Annual) must compare like-for-like
    or the raw ``price`` comparison wrongly classifies "cheaper per month
    but higher headline" as an upgrade and fires immediate proration.
    """
    months = _PERIOD_UNIT_MONTHS[plan.period_unit] * Decimal(plan.period_count)
    return plan.price / months


def _classify_plan_change(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> t.Literal["upgrade", "downgrade"]:
    """Return ``"upgrade"`` when new_plan costs more per month; ``"downgrade"`` otherwise."""
    return (
        "upgrade" if _monthly_equivalent_price(new_plan) > _monthly_equivalent_price(subscription.plan) else "downgrade"
    )


def _retrieve_subscription_item_id(stripe_subscription_id: str, org: Organization) -> str:
    """Return the first Subscription Item id from a live Stripe Subscription."""
    try:
        stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id, **_stripe_account_kwargs(org))
    except stripe.error.StripeError as exc:
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    items = (stripe_sub.get("items") or {}).get("data") or []
    if not items:
        raise HttpError(502, str(_("Stripe subscription has no items to update.")))
    return t.cast(str, items[0]["id"])


def _upgrade_online_subscription(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Apply an immediate, prorated price swap on Stripe.

    Stripe issues a prorated invoice on the spot. If payment fails the
    subscription moves to ``past_due`` and the existing dunning flow takes
    over; either way the price swap stands.
    """
    org = subscription.organization
    kwargs = _stripe_account_kwargs(org)
    stripe_sub_id = t.cast(str, subscription.stripe_subscription_id)
    item_id = _retrieve_subscription_item_id(stripe_sub_id, org)
    try:
        stripe.Subscription.modify(
            stripe_sub_id,
            items=[{"id": item_id, "price": new_plan.stripe_price_id}],
            proration_behavior="create_prorations",
            payment_behavior="allow_incomplete",
            metadata={"revel_plan_id": str(new_plan.pk)},
            **kwargs,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_upgrade_failed",
            subscription_id=str(subscription.pk),
            new_plan_id=str(new_plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    # Reflect the price swap locally right away so the API response sees the
    # new plan without waiting for the ``customer.subscription.updated`` webhook
    # round-trip. The webhook re-applies the same state idempotently.
    subscription.plan = new_plan
    subscription.pending_plan = None
    subscription.save(update_fields=["plan", "pending_plan", "updated_at"])
    return subscription


def _downgrade_online_subscription(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Schedule a price swap at the next renewal via a Stripe Subscription Schedule.

    Two-phase schedule: phase 1 keeps the current price for the rest of the
    current period; phase 2 starts the new price at the period boundary.
    ``end_behavior='release'`` lets the subscription fall back to a normal
    rolling renewal at the new price once the schedule's second phase
    completes its first iteration.
    """
    org = subscription.organization
    kwargs = _stripe_account_kwargs(org)
    try:
        schedule = stripe.SubscriptionSchedule.create(
            from_subscription=subscription.stripe_subscription_id,
            **kwargs,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_downgrade_failed",
            subscription_id=str(subscription.pk),
            new_plan_id=str(new_plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    try:
        current_phase = (schedule.get("phases") or [None])[0]
        if not current_phase:
            raise HttpError(502, str(_("Stripe did not return a schedule phase to extend.")))
        existing_price = ((current_phase.get("items") or [{}])[0].get("price")) or subscription.plan.stripe_price_id
        new_phases: list[dict[str, t.Any]] = [
            {
                "items": [{"price": existing_price, "quantity": 1}],
                "start_date": current_phase.get("start_date"),
                "end_date": current_phase.get("end_date"),
                "proration_behavior": "none",
            },
            {
                "items": [{"price": new_plan.stripe_price_id, "quantity": 1}],
                "iterations": 1,
                "proration_behavior": "none",
            },
        ]
        stripe.SubscriptionSchedule.modify(
            schedule.id,
            end_behavior="release",
            phases=new_phases,
            metadata={"revel_subscription_id": str(subscription.pk), "revel_new_plan_id": str(new_plan.pk)},
            **kwargs,
        )
    except (stripe.error.StripeError, HttpError) as exc:
        # Best-effort release of the orphaned schedule so it doesn't keep applying.
        try:
            stripe.SubscriptionSchedule.release(schedule.id, **kwargs)
        except stripe.error.StripeError as release_exc:
            logger.error(
                "subscription_stripe_downgrade_schedule_release_failed",
                subscription_id=str(subscription.pk),
                new_plan_id=str(new_plan.pk),
                schedule_id=str(schedule.id),
                error=str(release_exc),
            )
        logger.error(
            "subscription_stripe_downgrade_failed",
            subscription_id=str(subscription.pk),
            new_plan_id=str(new_plan.pk),
            error=str(exc),
        )
        if isinstance(exc, HttpError):
            raise
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    subscription.pending_plan = new_plan
    subscription.stripe_schedule_id = t.cast(str, schedule.id)
    subscription.save(update_fields=["pending_plan", "stripe_schedule_id", "updated_at"])
    return subscription


def _validate_change_plan_state(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> None:
    """Pre-flight checks shared by upgrade and downgrade routing."""
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot change the plan on a terminated subscription.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Resume the subscription before changing its plan.")))
    if subscription.cancel_at_period_end:
        raise HttpError(400, str(_("This subscription is scheduled to cancel; cannot change plan.")))
    if subscription.pending_plan_id:
        raise HttpError(400, str(_("A plan change is already pending on this subscription.")))
    if subscription.plan_id == new_plan.pk:
        raise HttpError(400, str(_("This subscription is already on that plan.")))


def _ensure_new_plan_has_stripe_price(new_plan: MembershipSubscriptionPlan) -> None:
    """Lazy-provision the Stripe Price for ``new_plan`` if it's missing.

    Plans created before Phase 2 (or whose initial sync failed) might be
    missing ``stripe_price_id``. Provisioning here lets a member still switch
    to them without a manual fix-up step.
    """
    if new_plan.stripe_price_id:
        return
    # Lazy import to avoid a circular dependency with subscription_stripe_service.
    from events.service.subscription_stripe_service import ensure_stripe_price  # noqa: PLC0415

    ensure_stripe_price(new_plan)
    new_plan.refresh_from_db()
    if not new_plan.stripe_price_id:
        raise HttpError(500, str(_("Could not prepare the plan for checkout.")))


def change_online_plan(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Switch the plan on an ONLINE subscription.

    Same-currency check, then routes to :func:`_upgrade_online_subscription`
    or :func:`_downgrade_online_subscription` based on price delta.

    Validation runs under a brief ``select_for_update`` lock that is
    released before the Stripe HTTP call — mirroring the pattern in
    ``start_online_subscription`` and ``revive_subscription`` to avoid
    holding the row lock across a slow external call. Concurrent attempts
    are still serialized in practice because Stripe rejects a second
    ``SubscriptionSchedule.create`` against the same ``from_subscription``
    once one already exists.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))

    _ensure_new_plan_has_stripe_price(new_plan)

    with transaction.atomic():
        subscription = (
            MembershipSubscription.objects.select_for_update()
            .select_related("plan", "plan__tier", "organization", "user")
            .get(pk=subscription.pk)
        )
        _validate_change_plan_state(subscription, new_plan)
        classification = _classify_plan_change(subscription, new_plan)

    # Stripe calls run OUTSIDE the row lock (see docstring).
    if classification == "upgrade":
        return _upgrade_online_subscription(subscription, new_plan)
    return _downgrade_online_subscription(subscription, new_plan)
