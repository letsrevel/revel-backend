"""Leaf predicates for "has this subscription ever been paid" (models only, never services)."""

import datetime
import typing as t

from django.db.models import Exists, Expression, F, OuterRef, QuerySet, Subquery
from django.db.models.functions import Greatest

from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan

#: Days a never-paid OFFLINE subscription may wait for its first payment before the daily sweep cancels it.
APPLICATION_PAYMENT_WINDOW_DAYS: t.Final[int] = 30

_QS = t.TypeVar("_QS", bound="QuerySet[MembershipSubscription]")


def annotate_last_payment_updated_at(qs: _QS) -> _QS:
    """Annotate ``last_payment_updated_at``: the latest ``updated_at`` over the subscription's payments (any status).

    The one place list endpoints and the expiry sweep read "last payment activity" from, so
    :func:`payment_due_by` costs no query per row.
    """
    latest = (
        MembershipPayment.objects.filter(subscription=OuterRef("pk")).order_by("-updated_at").values("updated_at")[:1]
    )
    return qs.annotate(last_payment_updated_at=Subquery(latest))


def annotate_payment_progress(qs: _QS) -> _QS:
    """Annotate ``has_succeeded_payment`` and ``last_payment_updated_at``, which the schemas read per row.

    Use it on every queryset (or nested prefetch) that serialises ``MySubscriptionSchema`` / ``SubscriptionSchema``.
    """
    succeeded = MembershipPayment.objects.filter(
        subscription=OuterRef("pk"), status=MembershipPayment.PaymentStatus.SUCCEEDED
    )
    return annotate_last_payment_updated_at(qs).annotate(has_succeeded_payment=Exists(succeeded))


def payment_anchor_expression() -> Expression:
    """DB form of the payment window's start: the later of creation and last payment activity.

    Needs :func:`annotate_last_payment_updated_at`. ``GREATEST`` ignores NULL, so a subscription
    without payments anchors on ``created_at``. The sweep compares it to ``now - window``;
    :func:`payment_due_by` is the same rule evaluated in Python.
    """
    return Greatest(F("created_at"), F("last_payment_updated_at"))


def payment_due_by(subscription: MembershipSubscription) -> datetime.datetime | None:
    """When a never-paid OFFLINE PENDING subscription is swept: its anchor plus the payment window.

    ``None`` for anything else. Same rule as ``expire_unpaid_offline_subscriptions`` (see
    :func:`payment_anchor_expression`): ``created_at + window``, postponed to the last payment's
    ``updated_at + window`` when that is later.
    """
    if not is_never_paid_offline(subscription):
        return None
    if hasattr(subscription, "last_payment_updated_at"):
        last = subscription.last_payment_updated_at
    else:
        last = (
            MembershipPayment.objects.filter(subscription=subscription)
            .order_by("-updated_at")
            .values_list("updated_at", flat=True)
            .first()
        )
    anchor = max(subscription.created_at, last) if last is not None else subscription.created_at
    return anchor + datetime.timedelta(days=APPLICATION_PAYMENT_WINDOW_DAYS)


def is_never_paid(subscription: MembershipSubscription) -> bool:
    """True when the subscription has no SUCCEEDED payment yet.

    List endpoints annotate ``has_succeeded_payment`` on the queryset to avoid one query per row.
    """
    annotated = getattr(subscription, "has_succeeded_payment", None)
    if annotated is not None:
        return not annotated
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
