"""Tier eligibility gates and subscription plans are mutually exclusive (#774 Phase 1).

Paid memberships never run the ``membership_manager`` gate stack, so a monetized
tier's ``requires_membership_approval`` / ``membership_questionnaire`` would be
configured but never enforced. Both directions of the mutation are refused; see
``docs/architecture/membership-eligibility.md``.
"""

from decimal import Decimal

import pytest
from ninja.errors import HttpError

from events import schema
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationQuestionnaire,
)
from events.service import organization_service, subscription_service
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """The default tier auto-created on organization save."""
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def membership_oq(organization: Organization) -> OrganizationQuestionnaire:
    """A MEMBERSHIP-type questionnaire owned by the organization."""
    questionnaire = Questionnaire.objects.create(
        name="Membership Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED
    )
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )


def _plan(tier: MembershipTier, *, name: str = "Monthly", is_active: bool = True) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier,
        name=name,
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        is_active=is_active,
    )


# ---- Tier side: enabling a gate on a monetized tier --------------------------


class TestTierGateConfigOnMonetizedTier:
    def test_requires_approval_rejected_when_tier_has_active_plan(self, tier: MembershipTier) -> None:
        _plan(tier)

        with pytest.raises(HttpError) as exc_info:
            organization_service.update_membership_tier(
                tier, schema.MembershipTierUpdateSchema(requires_membership_approval=True)
            )

        assert exc_info.value.status_code == 400
        tier.refresh_from_db()
        assert tier.requires_membership_approval is None

    def test_questionnaire_rejected_when_tier_has_active_plan(
        self, tier: MembershipTier, membership_oq: OrganizationQuestionnaire
    ) -> None:
        _plan(tier)

        with pytest.raises(HttpError) as exc_info:
            organization_service.update_membership_tier(
                tier, schema.MembershipTierUpdateSchema(membership_questionnaire_id=membership_oq.id)
            )

        assert exc_info.value.status_code == 400
        tier.refresh_from_db()
        assert tier.membership_questionnaire_id is None

    def test_archived_plan_does_not_monetize_the_tier(
        self, tier: MembershipTier, membership_oq: OrganizationQuestionnaire
    ) -> None:
        """``is_active=False`` sells nothing, so the gates stay configurable."""
        _plan(tier, is_active=False)

        updated = organization_service.update_membership_tier(
            tier,
            schema.MembershipTierUpdateSchema(
                requires_membership_approval=True, membership_questionnaire_id=membership_oq.id
            ),
        )

        assert updated.requires_membership_approval is True
        assert updated.membership_questionnaire_id == membership_oq.id

    def test_free_tier_gate_config_still_works(
        self, tier: MembershipTier, membership_oq: OrganizationQuestionnaire
    ) -> None:
        updated = organization_service.update_membership_tier(
            tier,
            schema.MembershipTierUpdateSchema(
                requires_membership_approval=True, membership_questionnaire_id=membership_oq.id
            ),
        )

        assert updated.requires_membership_approval is True
        assert updated.membership_questionnaire_id == membership_oq.id

    def test_disabling_gates_on_a_monetized_tier_is_allowed(self, tier: MembershipTier) -> None:
        """Only *enabling* a gate is refused — the escape hatch must stay open."""
        tier.requires_membership_approval = True
        tier.save(update_fields=["requires_membership_approval"])
        # Legacy row: gates set before the plan existed.
        MembershipSubscriptionPlan.objects.create(
            tier=tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month", period_count=1
        )

        updated = organization_service.update_membership_tier(
            tier,
            schema.MembershipTierUpdateSchema(requires_membership_approval=False, membership_questionnaire_id=None),
        )

        assert updated.requires_membership_approval is False

    def test_unrelated_edit_on_legacy_inconsistent_tier_is_allowed(self, tier: MembershipTier) -> None:
        tier.requires_membership_approval = True
        tier.save(update_fields=["requires_membership_approval"])
        MembershipSubscriptionPlan.objects.create(
            tier=tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month", period_count=1
        )

        updated = organization_service.update_membership_tier(tier, schema.MembershipTierUpdateSchema(name="Renamed"))

        assert updated.name == "Renamed"


# ---- Plan side: putting a plan on sale on a gated tier -----------------------


class TestPlanCreationOnGatedTier:
    def test_create_plan_rejected_when_tier_requires_approval(self, tier: MembershipTier) -> None:
        tier.requires_membership_approval = True
        tier.save(update_fields=["requires_membership_approval"])

        with pytest.raises(HttpError) as exc_info:
            _plan(tier)

        assert exc_info.value.status_code == 400
        assert not MembershipSubscriptionPlan.objects.filter(tier=tier).exists()

    def test_create_plan_rejected_when_tier_has_questionnaire(
        self, tier: MembershipTier, membership_oq: OrganizationQuestionnaire
    ) -> None:
        tier.membership_questionnaire = membership_oq
        tier.save(update_fields=["membership_questionnaire"])

        with pytest.raises(HttpError) as exc_info:
            _plan(tier)

        assert exc_info.value.status_code == 400
        assert not MembershipSubscriptionPlan.objects.filter(tier=tier).exists()

    def test_create_archived_plan_on_gated_tier_is_allowed(self, tier: MembershipTier) -> None:
        tier.requires_membership_approval = True
        tier.save(update_fields=["requires_membership_approval"])

        plan = _plan(tier, is_active=False)

        assert plan.is_active is False

    def test_clearing_the_gates_then_creating_a_plan_is_allowed(
        self, tier: MembershipTier, membership_oq: OrganizationQuestionnaire
    ) -> None:
        organization_service.update_membership_tier(
            tier,
            schema.MembershipTierUpdateSchema(
                requires_membership_approval=True, membership_questionnaire_id=membership_oq.id
            ),
        )
        tier.refresh_from_db()
        with pytest.raises(HttpError):
            _plan(tier)

        organization_service.update_membership_tier(
            tier,
            schema.MembershipTierUpdateSchema(requires_membership_approval=False, membership_questionnaire_id=None),
        )
        tier.refresh_from_db()

        assert _plan(tier).is_active is True

    def test_explicit_false_override_does_not_block_plan_creation(self, tier: MembershipTier) -> None:
        """A tier opting *out* of approval is coherent with being sold."""
        tier.requires_membership_approval = False
        tier.save(update_fields=["requires_membership_approval"])

        assert _plan(tier).pk

    def test_unarchiving_a_plan_on_a_gated_tier_is_rejected(self, tier: MembershipTier) -> None:
        """Archive → configure gates → un-archive must not reopen the inert-config state."""
        plan = _plan(tier, is_active=False)
        tier.requires_membership_approval = True
        tier.save(update_fields=["requires_membership_approval"])

        with pytest.raises(HttpError) as exc_info:
            subscription_service.update_plan(plan, is_active=True)

        assert exc_info.value.status_code == 400
        plan.refresh_from_db()
        assert plan.is_active is False

    def test_updating_an_active_plan_on_a_legacy_gated_tier_is_allowed(self, tier: MembershipTier) -> None:
        """The un-archive guard only fires on the False → True transition."""
        plan = _plan(tier)
        tier.requires_membership_approval = True
        tier.save(update_fields=["requires_membership_approval"])

        updated = subscription_service.update_plan(plan, is_active=True, name="Renamed")

        assert updated.name == "Renamed"
