"""Changes to a live Stripe subscription: the price update of a plan migration, pause and resume.

Split from ``checkout`` (#1473), which re-exports these names: callers and patches use ``checkout.<name>``.
"""

import typing as t

import stripe
import structlog
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)
from events.service.subscription.stripe.base import (
    ensure_stripe_price as ensure_stripe_price,
)
from events.service.subscription.stripe.base import (
    recurring_cadence,
)
from events.service.subscription.stripe.payloads import (
    stripe_account_kwargs,
)
from events.service.subscription.stripe.plan_change import (
    release_online_schedule,
)

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("events.service.subscription.stripe.checkout")


# ---- Plan changes (Phase 3) -------------------------------------------------
# Full implementation lives in subscription.stripe.plan_change.py.
# See subscription.lifecycle.change_plan for the dispatch entry-point.


CadenceLookup = t.Callable[[str, Organization], tuple[str, int] | None]


def fetch_price_cadence(price_id: str, organization: Organization) -> tuple[str, int] | None:
    """``(interval, interval_count)`` of a Stripe Price on ``organization``'s account, or None if it does not recur.

    Raises:
        HttpError: 502 when Stripe cannot return the Price.
    """
    try:
        price = stripe.Price.retrieve(price_id, **stripe_account_kwargs(organization))
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_target_price_retrieve_failed",
            organization_id=str(organization.pk),
            target_price_id=price_id,
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    return recurring_cadence(price)


def update_subscription_price(
    subscription: MembershipSubscription,
    cadence_of: CadenceLookup | None = None,
) -> bool:
    """Swap the Stripe Price to the plan's current price (proration_behavior='none').

    Callers must filter out schedule-managed rows (``stripe_schedule_id`` set)
    first — Stripe rejects a plain ``Subscription.modify`` while a schedule is
    attached, so this raises the module's 502 rather than migrating them. See
    ``subscription.plans.migrate_plan_subscribers`` and
    :func:`resync_subscription_application_fees` for the carve-out.

    ``cadence_of`` lets a caller that migrates many subscribers share Price
    lookups: it is called with the id of the Price being installed and the
    subscription's organization, and must return that Price's cadence as read on
    that organization's Stripe account.
    When None, the Price is retrieved here via :func:`fetch_price_cadence`.

    Returns True when modify was called; False when already current or skipped.

    Raises:
        ValueError: If the subscriber's Stripe Price and the plan's current
            Price recur on different intervals (or either does not recur);
            ``migrate_plan_subscribers`` reports it as a per-subscription failure.
        HttpError: 502 when Stripe cannot return a Price or the swap fails.
    """
    if not subscription.stripe_subscription_id:
        return False
    target_price_id = subscription.plan.stripe_price_id
    if not target_price_id:
        return False

    org = subscription.organization
    kwargs = stripe_account_kwargs(org)
    try:
        stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id, **kwargs)
    except stripe.error.StripeError as exc:
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    items = (stripe_sub.get("items") or {}).get("data") or []
    if not items:
        return False
    item = items[0]
    item_price = item.get("price") or {}
    if item_price.get("id") == target_price_id:
        return False
    # With proration_behavior="none", Stripe stretches the paid period to a new
    # interval without invoicing (#1423). Compare the two Prices themselves, not
    # the plan row, which an admin edit can leave out of sync with its Price.
    current_cadence = recurring_cadence(item_price)
    if cadence_of is None:
        plan_cadence = fetch_price_cadence(target_price_id, org)
    else:
        plan_cadence = cadence_of(target_price_id, org)
    if current_cadence is None or current_cadence != plan_cadence:
        raise ValueError(
            f"Billing period differs (current Price {current_cadence}, plan Price {plan_cadence}): not migrated. "
            "Move the subscriber to a plan with a matching billing period."
        )
    try:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            items=[{"id": item["id"], "price": target_price_id}],
            proration_behavior="none",
            **kwargs,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_price_swap_failed",
            subscription_id=str(subscription.pk),
            target_price_id=target_price_id,
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    return True


def pause_online_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Pause invoice collection on Stripe.

    Uses ``pause_collection.behavior='void'`` so any draft invoices created
    while paused are voided rather than sitting around. The subscription
    keeps its existing ``status`` on Stripe (``active``); we surface PAUSED
    locally so members and staff see a clear signal.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot pause a terminal subscription.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        return subscription

    org = subscription.organization

    # A pending downgrade makes the subscription schedule-managed on Stripe,
    # which rejects ``pause_collection``. Release the schedule first (clears
    # stripe_schedule_id + pending_plan locally) so the pause can proceed.
    release_online_schedule(subscription)

    try:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            pause_collection={"behavior": "void"},
            **stripe_account_kwargs(org),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_pause_failed",
            subscription_id=str(subscription.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    subscription.status = MembershipSubscription.SubscriptionStatus.PAUSED
    subscription.save(update_fields=["status", "updated_at"])
    return subscription


def resume_online_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Resume a previously paused Stripe subscription.

    Sending ``pause_collection=""`` clears the pause on Stripe; the local
    status flips back to ACTIVE. The grace-expiry Celery task will move it
    to PAST_DUE later if the period has already lapsed during the pause.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))
    if subscription.status != MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Only paused subscriptions can be resumed.")))

    org = subscription.organization
    try:
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            pause_collection="",
            **stripe_account_kwargs(org),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_resume_failed",
            subscription_id=str(subscription.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    subscription.save(update_fields=["status", "updated_at"])
    return subscription
