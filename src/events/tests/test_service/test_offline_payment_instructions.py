"""Plan/org payment instructions: effective-value resolution and API exposure."""

from decimal import Decimal

import pytest

from events.models import MembershipSubscriptionPlan, MembershipTier, Organization
from events.schema import PlanSchema, PublicPlanSchema
from events.service.subscription import plans as subscription_plans

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return subscription_plans.create_plan(
        tier, name="Annual", price=Decimal("50.00"), currency="EUR", period_unit="year", period_count=1
    )


def test_plan_instructions_win(plan: MembershipSubscriptionPlan, organization: Organization) -> None:
    organization.membership_payment_instructions = "Org IBAN"
    organization.save()
    plan.payment_instructions = "  Plan IBAN  "
    plan.save()
    assert plan.effective_payment_instructions == "Plan IBAN"


def test_falls_back_to_org(plan: MembershipSubscriptionPlan, organization: Organization) -> None:
    organization.membership_payment_instructions = " Org IBAN "
    organization.save()
    assert plan.payment_instructions is None
    assert plan.effective_payment_instructions == "Org IBAN"


def test_blank_plan_value_falls_back(plan: MembershipSubscriptionPlan, organization: Organization) -> None:
    organization.membership_payment_instructions = "Org IBAN"
    organization.save()
    plan.payment_instructions = "   "
    plan.save()
    assert plan.effective_payment_instructions == "Org IBAN"


def test_none_when_nothing_set(plan: MembershipSubscriptionPlan) -> None:
    assert plan.effective_payment_instructions is None


def test_public_plan_schema_hides_instructions() -> None:
    assert "payment_instructions" not in PublicPlanSchema.model_json_schema()["properties"]
    assert "payment_instructions" in PlanSchema.model_json_schema()["properties"]
