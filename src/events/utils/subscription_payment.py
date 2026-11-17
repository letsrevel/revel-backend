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


def is_never_paid_on_offline_plan(subscription: MembershipSubscription) -> bool:
    """True when no money ever changed hands on an OFFLINE plan's subscription, whatever its status now.

    Stricter than :func:`is_never_paid`: a REFUNDED payment counts as money moved (a refunded-then-cancelled
    subscription is a paid member leaving, not an unpaid request lapsing). VOIDED payments never count.
    """
    return subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE and not (
        MembershipPayment.objects.filter(
            subscription=subscription,
            status__in=(MembershipPayment.PaymentStatus.SUCCEEDED, MembershipPayment.PaymentStatus.REFUNDED),
        ).exists()
    )
