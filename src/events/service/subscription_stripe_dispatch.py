"""Notification dispatch helpers for Stripe subscription webhook events.

These functions handle gating of RENEWAL_SUCCEEDED, PAYMENT_FAILED, CANCELLATION_CONFIRMED,
and SUBSCRIPTION_EXPIRED notifications based on state transitions and webhook re-delivery.
"""

from datetime import timedelta

from django.utils import timezone

from events.models import MembershipSubscription
from events.service import subscription_service

# Stripe abandons an unpaid first subscription invoice after ~23h
# (``incomplete`` → ``incomplete_expired``). That, not the org's grace window,
# is the deadline a first-invoice failure must quote to the member.
_FIRST_INVOICE_RECOVERY_HOURS = 23


def _dispatch_sync_notifications(
    subscription: MembershipSubscription,
    *,
    prior_status: str,
    prior_cap: bool,
) -> None:
    """Dispatch cancellation/expiry notifications on actual local-state transitions (D3)."""
    S = MembershipSubscription.SubscriptionStatus
    T = MembershipSubscription.TERMINAL_STATUSES
    if not prior_cap and subscription.cancel_at_period_end and subscription.status not in T:
        subscription_service._dispatch_cancellation_confirmed(subscription, immediate=False)
    # ``not prior_cap``: a scheduled cancel reaching its period end already fired
    # CANCELLATION_CONFIRMED(immediate=False) — the deletion webhook must not
    # re-fire it as a false "cancelled effective immediately".
    if prior_status not in T and subscription.status == S.CANCELLED.value and not prior_cap:
        subscription_service._dispatch_cancellation_confirmed(subscription, immediate=True)
    if prior_status not in T and subscription.status == S.EXPIRED.value:
        subscription_service._dispatch_subscription_expired(subscription)


def _dispatch_invoice_notifications(
    subscription: MembershipSubscription,
    *,
    prior_status: str,
    succeeded: bool,
    payment_created: bool,
    payment_recovered: bool = False,
    billing_reason: str = "",
) -> None:
    """Dispatch RENEWAL_SUCCEEDED or PAYMENT_FAILED based on prior → final status transition.

    Args:
        subscription: The membership subscription.
        prior_status: The subscription status before payment processing.
        succeeded: True for invoice.paid; False for invoice.payment_failed.
        payment_created: True if the payment row was newly created; False if updated.
            Ensures duplicate dispatches on webhook re-delivery are suppressed.
        payment_recovered: True when this ``invoice.paid`` flipped an *existing*
            FAILED row to SUCCEEDED — i.e. a dunning/SCA recovery on the same
            invoice, where ``payment_created`` is False (the row already existed)
            but the member did just recover and is owed RENEWAL_SUCCEEDED. A plain
            paid-invoice redelivery has a prior SUCCEEDED row, so this stays False
            and the redelivery dedup is preserved.
        billing_reason: The invoice's Stripe ``billing_reason``. A mid-cycle
            upgrade invoices immediately (``subscription_update``) and must not
            be announced to the member as a renewal.
    """
    S = MembershipSubscription.SubscriptionStatus
    if succeeded:
        if (
            (payment_created or payment_recovered)
            and prior_status in {S.ACTIVE.value, S.PAST_DUE.value}
            and billing_reason != "subscription_update"
        ):
            subscription_service._dispatch_renewal_succeeded(subscription)
    elif payment_created and prior_status in {S.ACTIVE.value, S.PENDING.value}:
        # PENDING is the first-invoice case (async payment methods, SCA): the row
        # is moved to PAST_DUE just like ACTIVE, so per ADR-0015 the member is
        # told either way. Their deadline is not the org grace window, though —
        # Stripe abandons an unpaid first invoice after ~23h, and that is the
        # only window in which paying still rescues the subscription.
        if prior_status == S.PENDING.value:
            grace_period_end = timezone.now() + timedelta(hours=_FIRST_INVOICE_RECOVERY_HOURS)
        else:
            grace_period_end = (subscription.current_period_end or timezone.now()) + timedelta(
                days=subscription.organization.membership_grace_period_days
            )
        subscription_service._dispatch_payment_failed(subscription, grace_period_end=grace_period_end, is_online=True)
