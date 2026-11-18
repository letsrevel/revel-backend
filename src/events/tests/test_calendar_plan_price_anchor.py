"""A prorated first calendar period anchors the price notice on the full price it stood in for."""

import datetime
import typing as t
from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import plans as subscription_plans

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="cal_user", email="cal_user@example.com", password="pass")


def _migrate_with_payment(
    tier: MembershipTier,
    organization: Organization,
    subscriber: RevelUser,
    owner: RevelUser,
    **payment_fields: t.Any,
) -> list[dict[str, t.Any]]:
    from notifications.enums import NotificationType
    from notifications.models import Notification

    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Calendar",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_alignment=MembershipSubscriptionPlan.PeriodAlignment.CALENDAR,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )
    sub = MembershipSubscription.objects.create(
        user=subscriber,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    MembershipPayment.objects.create(
        subscription=sub,
        currency="EUR",
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_end=datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc),
        **payment_fields,
    )
    plan.price = Decimal("15")
    plan.save(update_fields=["price"])

    result = subscription_plans.migrate_plan_subscribers(plan, initiated_by=owner)
    assert result["migrated"] == 1
    return [
        n.context
        for n in Notification.objects.filter(
            user=subscriber, notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE
        )
    ]


def test_prorated_first_payment_anchors_on_full_price(
    tier: MembershipTier,
    organization: Organization,
    subscriber: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    """A mid-period joiner is told the full price they joined under, not the prorated amount."""
    contexts = _migrate_with_payment(
        tier,
        organization,
        subscriber,
        organization_owner_user,
        amount=Decimal("2"),
        full_period_amount=Decimal("10"),
        period_start=datetime.datetime(2026, 4, 28, 12, tzinfo=datetime.timezone.utc),
    )
    assert len(contexts) == 1
    assert Decimal(contexts[0]["old_amount"].split()[0]) == Decimal("10")


def test_payment_without_full_period_amount_anchors_on_its_own_amount(
    tier: MembershipTier,
    organization: Organization,
    subscriber: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    """A row with no ``full_period_amount`` anchors the notice on what was paid, wherever its period starts."""
    contexts = _migrate_with_payment(
        tier,
        organization,
        subscriber,
        organization_owner_user,
        amount=Decimal("8"),
        period_start=datetime.datetime(2026, 4, 12, tzinfo=datetime.timezone.utc),
    )
    assert len(contexts) == 1
    assert Decimal(contexts[0]["old_amount"].split()[0]) == Decimal("8")
