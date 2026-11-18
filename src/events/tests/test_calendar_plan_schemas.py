"""Serialized output of the plan schemas that carry calendar-alignment fields."""

import datetime
from decimal import Decimal

import pytest
from freezegun import freeze_time

from events.models import MembershipSubscriptionPlan, MembershipTier, Organization
from events.schema.organization import MembershipRequestPlanSchema
from events.schema.subscription_plan import MemberPlanSchema, PlanSchema, PublicPlanSchema

Plan = MembershipSubscriptionPlan


@pytest.fixture
def calendar_plan(organization: Organization) -> Plan:
    tier = MembershipTier.objects.create(organization=organization, name="Pro")
    return Plan.objects.create(
        tier=tier,
        name="Season",
        price=Decimal("365.00"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.YEAR,
        period_alignment=Plan.PeriodAlignment.CALENDAR,
        calendar_start_month=9,
        payment_method=Plan.PaymentMethod.OFFLINE,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("schema", [PlanSchema, PublicPlanSchema])
def test_plan_schemas_serialize_first_period_from_one_instant(
    schema: type[PlanSchema | PublicPlanSchema], calendar_plan: Plan
) -> None:
    with freeze_time("2027-03-03 00:00:00"):
        data = schema.model_validate(calendar_plan).model_dump()
    assert data["period_alignment"] == "calendar"
    assert data["calendar_start_month"] == 9
    assert data["first_period_pricing"] == "prorated"
    assert data["first_period_end"] == datetime.datetime(2027, 9, 1, tzinfo=datetime.timezone.utc)
    assert data["first_period_price"] == Decimal("182.00")


@pytest.mark.django_db
def test_plan_schema_rolling_plan_has_no_first_period(calendar_plan: Plan) -> None:
    rolling = Plan.objects.create(
        tier=calendar_plan.tier,
        name="Rolling",
        price=Decimal("10"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.MONTH,
        payment_method=Plan.PaymentMethod.OFFLINE,
    )
    data = PlanSchema.model_validate(rolling).model_dump()
    assert data["period_alignment"] == "rolling"
    assert data["first_period_end"] is None
    assert data["first_period_price"] is None


@pytest.mark.django_db
def test_member_plan_schema_has_no_join_now_quote(calendar_plan: Plan) -> None:
    data = MemberPlanSchema.model_validate(calendar_plan).model_dump()
    assert data["period_alignment"] == "calendar"
    assert data["calendar_start_month"] == 9
    assert data["first_period_pricing"] == "prorated"
    assert "first_period_end" not in data
    assert "first_period_price" not in data


@pytest.mark.django_db
def test_membership_request_plan_schema_exposes_alignment(calendar_plan: Plan) -> None:
    data = MembershipRequestPlanSchema.model_validate(calendar_plan).model_dump()
    assert data["period_alignment"] == "calendar"
    assert data["calendar_start_month"] == 9
