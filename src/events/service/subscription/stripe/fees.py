"""Platform application-fee maths for membership subscriptions.

``application_fee_percent`` is the only fee mechanism Stripe offers for
subscriptions, so the platform fee (and the VAT on it) is folded into a single
percent here, and :func:`resync_subscription_application_fees` pushes the
current value onto live subscriptions when an org's VAT status changes.

Split out of :mod:`events.service.subscription.stripe.checkout` (file-length
cap): checkout *spends* the percent, this module *computes and resyncs* it.
"""

import time
import typing as t
from decimal import ROUND_HALF_UP, Decimal

import stripe
import structlog
from django.conf import settings

from common.models import SiteSettings
from common.service.stripe_config import configure_stripe
from common.service.vat_utils import b2b_vat_context
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)
from events.service.subscription.stripe.payloads import stripe_account_kwargs

logger = structlog.get_logger(__name__)

configure_stripe()


def effective_application_fee_percent(org: Organization) -> Decimal | None:
    """Org fee percent grossed up with platform VAT when applicable.

    Tickets charge the org fee + VAT on the fee (``calculate_platform_fee_vat``
    adds VAT on top). ``application_fee_percent`` is the only fee mechanism for
    subscriptions (Stripe has no fixed/absolute variant), so the same economics
    are achieved by grossing the percent itself up. The fixed fee component
    (``org.platform_fee_fixed``) intentionally does NOT apply to subscriptions.

    Returns:
        The percent to send to Stripe, or ``None`` when no fee applies.
    """
    if not org.platform_fee_percent:
        return None
    if not org.stripe_account_id or org.stripe_account_id == settings.STRIPE_ACCOUNT:
        return None
    site = SiteSettings.get_solo()
    reverse_charge, rate = b2b_vat_context(
        org, site.platform_vat_country, site.platform_vat_rate, unknown_country_domestic=True
    )
    if reverse_charge or rate <= 0:
        return org.platform_fee_percent
    grossed = (org.platform_fee_percent * (1 + rate / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Stripe rejects percents above 100; the org field is capped at 100, so a
    # gross-up can theoretically overshoot.
    return min(grossed, Decimal("100"))


class FeeResyncCounters(t.TypedDict):
    """Telemetry counters returned by :func:`resync_subscription_application_fees`."""

    updated: int
    skipped_schedule_managed: int
    failed: int


def resync_subscription_application_fees(org: Organization, *, sleep_seconds: float = 0.0) -> FeeResyncCounters:
    """Push the org's *current* effective fee percent onto its live Stripe subscriptions.

    The grossed-up ``application_fee_percent`` is frozen into each Stripe
    Subscription at Checkout; when the org's VAT status later changes (VIES
    revalidation, VAT ID set/cleared, country change) the frozen percent stops
    matching the fee the ledger decomposition assumes. This resyncs every
    non-terminal ONLINE subscription to the value Checkout would send today.
    A ``None`` percent (fee-free org) clears the fee on Stripe (``""`` unsets).

    Schedule-managed subscriptions (pending downgrades) are **skipped**: Stripe
    rejects a plain ``Subscription.modify`` while a schedule is attached, and
    releasing the schedule would silently drop the pending plan change. They
    are counted so callers can surface them; re-run the
    ``resync_subscription_fees`` management command once the schedule releases.

    Per-subscription Stripe failures are logged and counted, not raised — one
    bad subscription must not strand the rest of the org's resync.

    Args:
        org: The organization whose subscriptions to resync.
        sleep_seconds: Optional pause between Stripe calls (rate limiting for
            large backfills; the org's subscriptions all live on one Connect
            account).

    Returns:
        Counters for updated / skipped (schedule-managed) / failed rows.
    """
    target = effective_application_fee_percent(org)
    kwargs = stripe_account_kwargs(org)
    subscriptions = (
        MembershipSubscription.objects.filter(
            organization=org,
            plan__payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        .exclude(stripe_subscription_id="")
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
    )
    counters: FeeResyncCounters = {"updated": 0, "skipped_schedule_managed": 0, "failed": 0}
    for subscription in subscriptions:
        if subscription.stripe_schedule_id:
            counters["skipped_schedule_managed"] += 1
            logger.warning(
                "subscription_fee_resync_skipped_schedule_managed",
                subscription_id=str(subscription.pk),
                org_id=str(org.pk),
                schedule_id=subscription.stripe_schedule_id,
            )
            continue
        try:
            stripe.Subscription.modify(
                t.cast(str, subscription.stripe_subscription_id),
                application_fee_percent=float(target) if target is not None else "",
                **kwargs,
            )
        except stripe.error.StripeError as exc:
            counters["failed"] += 1
            logger.error(
                "subscription_fee_resync_failed",
                subscription_id=str(subscription.pk),
                org_id=str(org.pk),
                error=str(exc),
            )
            continue
        counters["updated"] += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
    logger.info(
        "subscription_fee_resync_done",
        org_id=str(org.pk),
        target_percent=str(target) if target is not None else None,
        **counters,
    )
    return counters
