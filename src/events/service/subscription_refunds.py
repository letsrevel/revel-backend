"""Refund handling for membership subscription payments.

Split out of :mod:`events.service.subscription_service` (file-length cap).
Serves both refund entry points: the ``charge.refunded`` webhook and
staff-recorded refunds from the org-admin API.
"""

from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan
from events.service import subscription_service, subscription_stripe_service

logger = structlog.get_logger(__name__)


def _is_full_refund_of_current_period(payment: MembershipPayment) -> bool:
    """Return True when the period covered by this payment has no remaining SUCCEEDED amount.

    Aggregates per-period totals AFTER ``refund_payment`` has flipped the row
    to REFUNDED. The current period's collected amount is the sum of all
    SUCCEEDED+REFUNDED amounts (originally-collected money); fully refunded
    means SUCCEEDED total is zero.

    Refunds against a historical period (not the subscription's current
    period_start) are bookkeeping — return False.
    """
    sub = payment.subscription
    if sub.current_period_start is None:
        return False
    if payment.period_start != sub.current_period_start:
        return False

    period_payments = MembershipPayment.objects.filter(
        subscription=sub,
        period_start=sub.current_period_start,
    )
    succeeded_total = period_payments.filter(status=MembershipPayment.PaymentStatus.SUCCEEDED).aggregate(
        s=Sum("amount")
    )["s"] or Decimal("0")
    refunded_total = period_payments.filter(status=MembershipPayment.PaymentStatus.REFUNDED).aggregate(s=Sum("amount"))[
        "s"
    ] or Decimal("0")
    return succeeded_total == Decimal("0") and refunded_total > Decimal("0")


def _cancel_refunded_subscription(subscription: MembershipSubscription) -> None:
    """Immediately terminalize a fully-refunded subscription.

    Deliberately not :func:`subscription_service.cancel_subscription`: the
    refund callers (the ``charge.refunded`` webhook, staff-recorded refunds)
    already hold row locks, and the ONLINE branch there issues the Stripe
    cancel synchronously — a network call under those locks. Here the local
    row is terminalized under the lock (local terminal state is authoritative;
    the terminal sync guard freezes it against later webhooks) and the
    Stripe-side cancel runs after commit as a best-effort call, mirroring the
    grace-expiry task — never hold a row lock across a network call.
    """
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization")
        .get(pk=subscription.pk)
    )
    if subscription.is_terminal:
        return
    subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
    subscription.cancelled_at = timezone.now()
    subscription.cancel_at_period_end = False
    subscription.save(update_fields=["status", "cancelled_at", "cancel_at_period_end", "updated_at"])
    if (
        subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE
        and subscription.stripe_subscription_id
    ):
        transaction.on_commit(
            lambda: subscription_stripe_service.cancel_stripe_subscription_best_effort(
                subscription, reason="refund_auto_cancel"
            )
        )
    subscription_service._dispatch_cancellation_confirmed(subscription, immediate=True)


@transaction.atomic
def refund_payment(
    payment: MembershipPayment,
    *,
    recorded_by: RevelUser | None,
    notes: str = "",
) -> MembershipPayment:
    """Mark a payment as refunded.

    If the refund fully covers the subscription's current period, also
    cancels the subscription immediately (the Stripe-side cancel is deferred
    to after commit — see :func:`_cancel_refunded_subscription`). Idempotent:
    re-calling for an already-REFUNDED payment is a no-op.
    """
    payment = MembershipPayment.objects.select_for_update().get(pk=payment.pk)
    if payment.status == MembershipPayment.PaymentStatus.REFUNDED:
        return payment
    payment.status = MembershipPayment.PaymentStatus.REFUNDED
    # Keep the audit trail consistent with the partial-refund path: a full
    # refund is still a refund, and organizers read these fields rather than
    # inferring the amount from ``status`` alone.
    payment.refund_amount = payment.amount
    payment.refunded_at = timezone.now()
    if notes:
        payment.notes = (payment.notes + ("\n" if payment.notes else "") + notes).strip()
    payment.save(update_fields=["status", "notes", "refund_amount", "refunded_at", "updated_at"])

    logger.info(
        "membership_payment_refunded",
        payment_id=str(payment.id),
        subscription_id=str(payment.subscription_id),
        recorded_by=str(recorded_by.id) if recorded_by else None,
    )

    # Phase 4: full refund of the current period auto-cancels the subscription.
    if _is_full_refund_of_current_period(payment):
        _cancel_refunded_subscription(payment.subscription)

    return payment
