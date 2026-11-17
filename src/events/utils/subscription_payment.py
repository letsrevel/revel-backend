"""Leaf predicates for "has this subscription ever been paid" (models only, never services)."""

from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan


def is_never_paid(subscription: MembershipSubscription) -> bool:
    """True when the subscription has no SUCCEEDED payment yet."""
    return not MembershipPayment.objects.filter(
        subscription=subscription, status=MembershipPayment.PaymentStatus.SUCCEEDED
    ).exists()


def is_never_paid_offline(subscription: MembershipSubscription) -> bool:
    """True for a PENDING, never-paid subscription on an OFFLINE plan (the one predicate for "first payment")."""
    return (
        subscription.status == MembershipSubscription.SubscriptionStatus.PENDING
        and subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE
        and is_never_paid(subscription)
    )
