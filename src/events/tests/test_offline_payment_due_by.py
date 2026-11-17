"""``payment_due_by`` and the unpaid-offline sweep share one rule: they must agree at the boundary."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    SubscriptionPaymentMethod,
)
from events.service.subscription.offline import open_offline_subscription
from events.tasks.subscriptions import expire_unpaid_offline_subscriptions
from events.utils.subscription_payment import (
    APPLICATION_PAYMENT_WINDOW_DAYS,
    annotate_last_payment_updated_at,
    annotate_payment_progress,
    payment_due_by,
)

pytestmark = pytest.mark.django_db

WINDOW = timedelta(days=APPLICATION_PAYMENT_WINDOW_DAYS)


@pytest.fixture
def plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit="year",
        payment_method=SubscriptionPaymentMethod.OFFLINE,
    )


@pytest.fixture(autouse=True)
def _quiet_notifications() -> t.Iterator[None]:
    with mock.patch("notifications.signals.notification_requested.send"):
        yield


def _backdate(sub: MembershipSubscription, *, created: timedelta, payment_updated: timedelta | None = None) -> None:
    now = timezone.now()
    MembershipSubscription.objects.filter(pk=sub.pk).update(created_at=now - created)
    if payment_updated is not None:
        payment = MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("50.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.VOIDED,
            period_start=now,
            period_end=now + WINDOW,
        )
        MembershipPayment.objects.filter(pk=payment.pk).update(updated_at=now - payment_updated)


@pytest.mark.parametrize(
    ("created", "payment_updated"),
    [
        (WINDOW + timedelta(hours=1), None),  # past due
        (WINDOW - timedelta(hours=1), None),  # still inside
        (WINDOW + timedelta(days=10), timedelta(hours=1)),  # old sub, fresh payment activity: postponed
        (WINDOW + timedelta(days=10), WINDOW + timedelta(hours=1)),  # old sub, stale payment: past due
        (timedelta(days=1), WINDOW + timedelta(days=5)),  # payment older than the sub: created_at anchors
    ],
)
def test_sweep_cancels_exactly_when_payment_due_by_has_passed(
    plan: MembershipSubscriptionPlan, nonmember_user: RevelUser, created: timedelta, payment_updated: timedelta | None
) -> None:
    sub = open_offline_subscription(plan, nonmember_user)
    _backdate(sub, created=created, payment_updated=payment_updated)
    due = payment_due_by(MembershipSubscription.objects.get(pk=sub.pk))
    annotated_due = payment_due_by(
        annotate_last_payment_updated_at(MembershipSubscription.objects.all()).get(pk=sub.pk)
    )
    assert due is not None
    assert annotated_due == due  # the list-endpoint path and the fallback path agree

    swept = expire_unpaid_offline_subscriptions()

    sub.refresh_from_db()
    assert (swept == 1) is (due < timezone.now())
    assert (sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED) is (due < timezone.now())


def test_payment_due_by_is_none_when_not_a_pending_offline_sub(plan: MembershipSubscriptionPlan) -> None:
    from events.service.subscription import lifecycle

    user = RevelUser.objects.create_user(username="p@example.com", email="p@example.com", password="x")
    sub = open_offline_subscription(plan, user)
    lifecycle.record_payment(sub, amount=Decimal("50.00"), currency="EUR", recorded_by=None)
    sub.refresh_from_db()
    assert payment_due_by(sub) is None


def test_payment_due_by_is_none_after_a_refunded_payment(plan: MembershipSubscriptionPlan) -> None:
    """The sweep never touches a subscription whose money was refunded, so no due date is promised."""
    user = RevelUser.objects.create_user(username="r@example.com", email="r@example.com", password="x")
    sub = open_offline_subscription(plan, user)
    MembershipPayment.objects.create(
        subscription=sub,
        amount=Decimal("50.00"),
        currency="EUR",
        status=MembershipPayment.PaymentStatus.REFUNDED,
        period_start=timezone.now(),
        period_end=timezone.now() + WINDOW,
    )
    assert payment_due_by(MembershipSubscription.objects.get(pk=sub.pk)) is None
    assert payment_due_by(annotate_payment_progress(MembershipSubscription.objects.all()).get(pk=sub.pk)) is None
