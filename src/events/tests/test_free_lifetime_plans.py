"""Tests for FREE payment method + LIFETIME period unit (issue #832).

Covers the plan-shape validation matrix (create schema and update service),
lifetime period arithmetic, the beats' NULL-period-end blind spot, MRR
normalisation, staff-created FREE subscriptions, and cancelling one.
"""

import datetime
import typing as t
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError
from pydantic import ValidationError

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    SubscriptionPaymentMethod,
)
from events.schema.subscription import PlanCreateSchema
from events.service import subscription_service
from events.service.subscription_core import create_subscription
from events.service.subscription_reporting import _normalize_to_monthly
from events.tasks.subscriptions import expire_subscriptions_past_grace, send_subscription_renewal_reminders
from events.utils.subscription_periods import calculate_period_end

pytestmark = pytest.mark.django_db

PeriodUnit = MembershipSubscriptionPlan.PeriodUnit


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


def _payload(**overrides: t.Any) -> dict[str, t.Any]:
    base: dict[str, t.Any] = {
        "name": "Plan",
        "price": Decimal("0"),
        "currency": "EUR",
        "period_unit": PeriodUnit.LIFETIME,
        "payment_method": SubscriptionPaymentMethod.FREE,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Create-time validation matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"price": Decimal("5.00")}, "price of 0"),
        ({"period_unit": PeriodUnit.MONTH}, "lifetime billing period"),
        (
            {"payment_method": SubscriptionPaymentMethod.ONLINE, "price": Decimal("0")},
            "greater than 0",
        ),
        (
            {"payment_method": SubscriptionPaymentMethod.ONLINE, "price": Decimal("10.00")},
            "cannot use the lifetime",
        ),
    ],
)
def test_plan_create_schema_refuses_incoherent_shapes(overrides: dict[str, t.Any], expected_fragment: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        PlanCreateSchema(**_payload(**overrides))
    assert expected_fragment in str(exc_info.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # FREE + LIFETIME + price 0
        {"payment_method": SubscriptionPaymentMethod.OFFLINE, "price": Decimal("99.00")},  # paid lifetime
        {"payment_method": SubscriptionPaymentMethod.OFFLINE, "price": Decimal("0")},  # free-ish offline
        {
            "payment_method": SubscriptionPaymentMethod.ONLINE,
            "price": Decimal("10.00"),
            "period_unit": PeriodUnit.MONTH,
        },
    ],
)
def test_plan_create_schema_accepts_coherent_shapes(overrides: dict[str, t.Any]) -> None:
    schema = PlanCreateSchema(**_payload(**overrides))
    assert schema.name == "Plan"


def test_create_free_lifetime_plan_end_to_end(tier: MembershipTier) -> None:
    payload = PlanCreateSchema(**_payload(name="Supporter"))
    plan = subscription_service.create_plan(tier, **payload.model_dump())
    assert plan.payment_method == SubscriptionPaymentMethod.FREE
    assert plan.period_unit == PeriodUnit.LIFETIME
    assert plan.price == Decimal("0")
    # No Stripe objects: FREE never touches Stripe.
    assert plan.stripe_price_id == ""


# --------------------------------------------------------------------------- #
# Update-time validation matrix
# --------------------------------------------------------------------------- #


@pytest.fixture
def free_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Supporter",
        price=Decimal("0"),
        currency="EUR",
        period_unit=PeriodUnit.LIFETIME,
        payment_method=SubscriptionPaymentMethod.FREE,
    )


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=PeriodUnit.MONTH,
        payment_method=SubscriptionPaymentMethod.ONLINE,
    )


def test_update_refuses_free_plan_going_paid(free_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_service.update_plan(free_plan, price=Decimal("5.00"))
    assert "price of 0" in str(exc_info.value)


def test_update_refuses_free_plan_leaving_lifetime(free_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_service.update_plan(free_plan, period_unit=PeriodUnit.MONTH)
    assert "lifetime billing period" in str(exc_info.value)


def test_update_refuses_online_plan_going_free(online_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_service.update_plan(online_plan, price=Decimal("0"))
    assert "greater than 0" in str(exc_info.value)


def test_update_refuses_online_plan_going_lifetime(online_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_service.update_plan(online_plan, period_unit=PeriodUnit.LIFETIME)
    assert "cannot use the lifetime" in str(exc_info.value)


def test_update_allows_offline_plan_going_lifetime(tier: MembershipTier) -> None:
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit=PeriodUnit.YEAR,
        payment_method=SubscriptionPaymentMethod.OFFLINE,
    )
    updated = subscription_service.update_plan(plan, period_unit=PeriodUnit.LIFETIME)
    assert updated.period_unit == PeriodUnit.LIFETIME


def test_update_allows_unrelated_field_on_free_plan(free_plan: MembershipSubscriptionPlan) -> None:
    updated = subscription_service.update_plan(free_plan, name="Friends")
    assert updated.name == "Friends"


# --------------------------------------------------------------------------- #
# Period arithmetic
# --------------------------------------------------------------------------- #


def test_calculate_period_end_returns_none_for_lifetime(free_plan: MembershipSubscriptionPlan) -> None:
    assert calculate_period_end(timezone.now(), free_plan) is None


# --------------------------------------------------------------------------- #
# Beats leave lifetime subscriptions alone
# --------------------------------------------------------------------------- #


def test_beats_never_touch_an_active_lifetime_subscription(
    free_plan: MembershipSubscriptionPlan,
    organization: Organization,
    user: RevelUser,
) -> None:
    """Expiry and reminder sweeps must ignore a NULL ``current_period_end``.

    Both beats filter on ``current_period_end`` (``__lt`` and ``__date``), which
    SQL NULL never satisfies — this pins that so a future refactor to e.g.
    ``exclude(current_period_end__gte=now)`` (which DOES select NULLs) cannot
    silently start expiring lifetime memberships.
    """
    subscription = MembershipSubscription.objects.create(
        user=user,
        plan=free_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=timezone.now() - datetime.timedelta(days=900),
        current_period_end=None,
    )

    expiry_counters = expire_subscriptions_past_grace()
    reminder_counters = send_subscription_renewal_reminders()

    subscription.refresh_from_db()
    assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
    assert subscription.current_period_end is None
    assert subscription.expired_at is None
    assert expiry_counters["past_due"] == 0
    assert expiry_counters["expired_after_grace"] == 0
    assert expiry_counters["cancelled_at_period_end"] == 0
    assert reminder_counters["sent"] == 0


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def test_lifetime_plan_contributes_zero_mrr(free_plan: MembershipSubscriptionPlan) -> None:
    # Even a *paid* lifetime plan (OFFLINE) is a one-off, not recurring revenue.
    free_plan.period_unit = PeriodUnit.LIFETIME
    assert _normalize_to_monthly(Decimal("120.00"), free_plan) == Decimal("0")


# --------------------------------------------------------------------------- #
# Staff-created FREE subscription + cancellation
# --------------------------------------------------------------------------- #


def test_staff_created_free_subscription_lands_active_with_member(
    free_plan: MembershipSubscriptionPlan,
    organization: Organization,
    user: RevelUser,
) -> None:
    subscription = create_subscription(free_plan, user)
    assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
    assert subscription.current_period_start is not None
    assert subscription.current_period_end is None
    member = OrganizationMember.objects.get(organization=organization, user=user)
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE
    assert member.tier_id == free_plan.tier_id


def test_cancelling_a_free_subscription_terminalizes_immediately(
    free_plan: MembershipSubscriptionPlan,
    organization: Organization,
    user: RevelUser,
) -> None:
    """A NULL period end has no boundary to wait for, so a scheduled cancel becomes immediate."""
    subscription = create_subscription(free_plan, user)
    cancelled = subscription_service.cancel_subscription(subscription, immediate=False)
    assert cancelled.status == MembershipSubscription.SubscriptionStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    assert cancelled.cancel_at_period_end is False
    member = OrganizationMember.objects.get(organization=organization, user=user)
    assert member.status == OrganizationMember.MembershipStatus.CANCELLED
