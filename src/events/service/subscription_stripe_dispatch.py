"""Notification dispatch helpers for Stripe subscription webhook events.

These functions handle gating of RENEWAL_SUCCEEDED, PAYMENT_FAILED, CANCELLATION_CONFIRMED,
and SUBSCRIPTION_EXPIRED notifications based on state transitions and webhook re-delivery.
"""

from datetime import timedelta

from django.utils import timezone

from events.models import MembershipSubscription
from events.service import subscription_service


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
    if prior_status not in T and subscription.status == S.CANCELLED.value:
        subscription_service._dispatch_cancellation_confirmed(subscription, immediate=True)
    if prior_status not in T and subscription.status == S.EXPIRED.value:
        subscription_service._dispatch_subscription_expired(subscription)


def _dispatch_invoice_notifications(
    subscription: MembershipSubscription,
    *,
    prior_status: str,
    succeeded: bool,
    payment_created: bool,
) -> None:
    """Dispatch RENEWAL_SUCCEEDED or PAYMENT_FAILED based on prior → final status transition.

    Args:
        subscription: The membership subscription.
        prior_status: The subscription status before payment processing.
        succeeded: True for invoice.paid; False for invoice.payment_failed.
        payment_created: True if the payment row was newly created; False if updated.
            Ensures duplicate dispatches on webhook re-delivery are suppressed.
    """
    S = MembershipSubscription.SubscriptionStatus
    if succeeded:
        if payment_created and prior_status in {S.ACTIVE.value, S.PAST_DUE.value}:
            subscription_service._dispatch_renewal_succeeded(subscription)
    elif payment_created and prior_status == S.ACTIVE.value:
        grace_period_end = (subscription.current_period_end or timezone.now()) + timedelta(
            days=subscription.organization.membership_grace_period_days
        )
        subscription_service._dispatch_payment_failed(subscription, grace_period_end=grace_period_end, is_online=True)
