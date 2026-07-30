"""Phase 2: eligibility gates and subscription plans coexist on a tier.

Replaces ``test_tier_gates_vs_plans.py``, which pinned the Phase-1 mutual
exclusion (both guards are deleted): ``/subscribe`` now runs the gate stack, so
gate config on a monetized tier is enforced rather than inert.
"""

from decimal import Decimal

import pytest

from events.models import MembershipSubscriptionPlan, MembershipTier, Organization, OrganizationQuestionnaire
from events.schema import MembershipTierUpdateSchema
from events.service import subscription_service
from events.service.organization_service import membership as membership_service
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


def _membership_questionnaire(organization: Organization) -> OrganizationQuestionnaire:
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=Questionnaire.objects.create(name="Membership Q"),
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )


def test_active_plan_on_gated_tier_is_allowed(organization: Organization, tier: MembershipTier) -> None:
    tier.requires_membership_approval = True
    tier.membership_questionnaire = _membership_questionnaire(organization)
    tier.save(update_fields=["requires_membership_approval", "membership_questionnaire"])

    plan = subscription_service.create_plan(
        tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
    )
    assert plan.is_active is True


def test_enabling_gates_on_monetized_tier_is_allowed(organization: Organization, tier: MembershipTier) -> None:
    MembershipSubscriptionPlan.objects.create(
        tier=tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )
    org_q = _membership_questionnaire(organization)

    updated = membership_service.update_membership_tier(
        tier,
        MembershipTierUpdateSchema(requires_membership_approval=True, membership_questionnaire_id=org_q.id),
    )
    assert updated.requires_membership_approval is True
    assert updated.membership_questionnaire_id == org_q.id
