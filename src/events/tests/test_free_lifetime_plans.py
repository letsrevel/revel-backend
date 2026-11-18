"""Tests for FREE payment method + LIFETIME period unit (issue #832).

Covers the plan-shape validation matrix (create schema and update service),
lifetime period arithmetic, the beats' NULL-period-end blind spot, MRR
normalisation, staff-created FREE subscriptions, and cancelling one.
"""

import datetime
import typing as t
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
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
from events.schema.subscription_plan import PlanCreateSchema
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans
from events.service.subscription.core import create_subscription
from events.service.subscription.reporting import _normalize_to_monthly
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
    plan = subscription_plans.create_plan(tier, **payload.model_dump())
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
        subscription_plans.update_plan(free_plan, price=Decimal("5.00"))
    assert "price of 0" in str(exc_info.value)


def test_update_refuses_free_plan_leaving_lifetime(free_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(free_plan, period_unit=PeriodUnit.MONTH)
    assert "lifetime billing period" in str(exc_info.value)


def test_update_refuses_online_plan_going_free(online_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(online_plan, price=Decimal("0"))
    assert "greater than 0" in str(exc_info.value)


def test_update_refuses_online_plan_going_lifetime(online_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(online_plan, period_unit=PeriodUnit.LIFETIME)
    assert "cannot use the lifetime" in str(exc_info.value)


def test_update_refuses_null_price(online_plan: MembershipSubscriptionPlan) -> None:
    """An explicit ``{"price": null}`` is a present key holding None — a 400, not a TypeError."""
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(online_plan, price=None)
    assert "cannot be null" in str(exc_info.value)


def test_update_allows_offline_plan_going_lifetime(tier: MembershipTier) -> None:
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit=PeriodUnit.YEAR,
        payment_method=SubscriptionPaymentMethod.OFFLINE,
    )
    updated = subscription_plans.update_plan(plan, period_unit=PeriodUnit.LIFETIME)
    assert updated.period_unit == PeriodUnit.LIFETIME


def test_update_allows_unrelated_field_on_free_plan(free_plan: MembershipSubscriptionPlan) -> None:
    updated = subscription_plans.update_plan(free_plan, name="Friends")
    assert updated.name == "Friends"


# --------------------------------------------------------------------------- #
# Model-level enforcement (defense-in-depth behind the API validators)
# --------------------------------------------------------------------------- #


def test_direct_orm_save_refuses_priced_free_plan(tier: MembershipTier) -> None:
    """``TimeStampedModel.save`` runs ``full_clean``, so admin/shell writes are covered too."""
    with pytest.raises(DjangoValidationError) as exc_info:
        MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Sneaky",
            price=Decimal("5.00"),
            currency="EUR",
            period_unit=PeriodUnit.LIFETIME,
            payment_method=SubscriptionPaymentMethod.FREE,
        )
    assert "price of 0" in str(exc_info.value)
    assert not MembershipSubscriptionPlan.objects.filter(name="Sneaky").exists()


def test_direct_orm_save_refuses_lifetime_online_plan(tier: MembershipTier) -> None:
    with pytest.raises(DjangoValidationError) as exc_info:
        MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Forever",
            price=Decimal("99.00"),
            currency="EUR",
            period_unit=PeriodUnit.LIFETIME,
            payment_method=SubscriptionPaymentMethod.ONLINE,
        )
    assert "cannot use the lifetime" in str(exc_info.value)


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
    cancelled = subscription_lifecycle.cancel_subscription(subscription, immediate=False)
    assert cancelled.status == MembershipSubscription.SubscriptionStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    assert cancelled.cancel_at_period_end is False
    member = OrganizationMember.objects.get(organization=organization, user=user)
    assert member.status == OrganizationMember.MembershipStatus.CANCELLED


# --------------------------------------------------------------------------- #
# Calendar-aligned plans: shape rules and immutability
# --------------------------------------------------------------------------- #

Alignment = MembershipSubscriptionPlan.PeriodAlignment


@pytest.mark.parametrize(
    "overrides, expected_fragment",
    [
        # Calendar + lifetime (hence never FREE).
        ({"period_alignment": Alignment.CALENDAR}, "monthly or yearly"),
        (
            {
                "period_alignment": Alignment.CALENDAR,
                "period_unit": PeriodUnit.LIFETIME,
                "payment_method": SubscriptionPaymentMethod.OFFLINE,
                "price": Decimal("5"),
            },
            "monthly or yearly",
        ),
        (
            {
                "period_alignment": Alignment.CALENDAR,
                "period_unit": PeriodUnit.MONTH,
                "period_count": 3,
                "payment_method": SubscriptionPaymentMethod.OFFLINE,
                "price": Decimal("5"),
            },
            "period count of 1",
        ),
        # Start month != 1 only on calendar YEAR plans.
        (
            {
                "period_unit": PeriodUnit.YEAR,
                "calendar_start_month": 9,
                "payment_method": SubscriptionPaymentMethod.OFFLINE,
                "price": Decimal("5"),
            },
            "only allowed on calendar yearly",
        ),
        (
            {
                "period_alignment": Alignment.CALENDAR,
                "period_unit": PeriodUnit.MONTH,
                "calendar_start_month": 9,
                "payment_method": SubscriptionPaymentMethod.OFFLINE,
                "price": Decimal("5"),
            },
            "only allowed on calendar yearly",
        ),
        # ONLINE is prorated-only.
        (
            {
                "payment_method": SubscriptionPaymentMethod.ONLINE,
                "price": Decimal("10"),
                "period_unit": PeriodUnit.MONTH,
                "first_period_pricing": MembershipSubscriptionPlan.FirstPeriodPricing.FULL,
            },
            "only prorate the first period",
        ),
    ],
)
def test_calendar_shape_refusals(overrides: dict[str, t.Any], expected_fragment: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        PlanCreateSchema(**_payload(**overrides))
    assert expected_fragment in str(exc_info.value)


@pytest.mark.parametrize(("month", "valid"), [(0, False), (1, True), (12, True), (13, False)])
def test_calendar_start_month_bounds(month: int, valid: bool) -> None:
    payload = _payload(
        period_unit=PeriodUnit.YEAR,
        period_alignment=Alignment.CALENDAR,
        calendar_start_month=month,
        payment_method=SubscriptionPaymentMethod.OFFLINE,
        price=Decimal("5"),
    )
    if valid:
        assert PlanCreateSchema(**payload).calendar_start_month == month
    else:
        with pytest.raises(ValidationError) as exc_info:
            PlanCreateSchema(**payload)
        assert "calendar_start_month" in str(exc_info.value)


def test_direct_orm_save_refuses_calendar_lifetime_plan(tier: MembershipTier) -> None:
    with pytest.raises(DjangoValidationError):
        MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Bad",
            price=Decimal("5.00"),
            currency="EUR",
            period_unit=PeriodUnit.LIFETIME,
            period_alignment=Alignment.CALENDAR,
            payment_method=SubscriptionPaymentMethod.OFFLINE,
        )


@pytest.fixture
def calendar_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    payload = PlanCreateSchema(
        **_payload(
            name="Season",
            price=Decimal("120.00"),
            period_unit=PeriodUnit.YEAR,
            period_alignment=Alignment.CALENDAR,
            calendar_start_month=9,
            payment_method=SubscriptionPaymentMethod.OFFLINE,
        )
    )
    return subscription_plans.create_plan(tier, **payload.model_dump())


def test_create_calendar_plan_persists_fields(calendar_plan: MembershipSubscriptionPlan) -> None:
    calendar_plan.refresh_from_db()
    assert calendar_plan.period_alignment == Alignment.CALENDAR
    assert calendar_plan.calendar_start_month == 9
    assert calendar_plan.first_period_pricing == MembershipSubscriptionPlan.FirstPeriodPricing.PRORATED


def test_update_refuses_period_unit_change_on_calendar_plan(calendar_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(calendar_plan, period_unit=PeriodUnit.MONTH)
    assert exc_info.value.status_code == 400
    assert "calendar plan cannot be changed" in str(exc_info.value)


def test_update_refuses_period_count_change_on_calendar_plan(calendar_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(calendar_plan, period_count=2)
    assert "period count of 1" in str(exc_info.value)


def test_update_allows_first_period_pricing_on_calendar_plan(calendar_plan: MembershipSubscriptionPlan) -> None:
    updated = subscription_plans.update_plan(
        calendar_plan, first_period_pricing=MembershipSubscriptionPlan.FirstPeriodPricing.FULL
    )
    assert updated.first_period_pricing == MembershipSubscriptionPlan.FirstPeriodPricing.FULL


def test_update_refuses_full_first_period_pricing_on_online_plan(online_plan: MembershipSubscriptionPlan) -> None:
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(
            online_plan, first_period_pricing=MembershipSubscriptionPlan.FirstPeriodPricing.FULL
        )
    assert exc_info.value.status_code == 400
    assert "only prorate the first period" in str(exc_info.value)


def test_update_refuses_null_first_period_pricing(calendar_plan: MembershipSubscriptionPlan) -> None:
    """An explicit ``{"first_period_pricing": null}`` is refused with a 400 by the shape guard."""
    with pytest.raises(HttpError) as exc_info:
        subscription_plans.update_plan(calendar_plan, first_period_pricing=None)
    assert exc_info.value.status_code == 400
    assert "cannot be null" in str(exc_info.value)
