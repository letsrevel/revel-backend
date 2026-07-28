"""Tests for membership-policy resolution helpers."""

import pytest

from events.models import MembershipTier, Organization, OrganizationQuestionnaire
from events.service.membership_manager.resolvers import (
    resolve_membership_questionnaire,
    resolve_requires_membership_approval,
)
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_questionnaire_a(organization: Organization) -> OrganizationQuestionnaire:
    q = Questionnaire.objects.create(name="Org default", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )


@pytest.fixture
def org_questionnaire_b(organization: Organization) -> OrganizationQuestionnaire:
    q = Questionnaire.objects.create(name="Tier-specific", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


def test_questionnaire_resolves_to_none_when_neither_configured(
    organization: Organization, tier: MembershipTier
) -> None:
    assert resolve_membership_questionnaire(organization, tier) is None
    assert resolve_membership_questionnaire(organization, None) is None


def test_questionnaire_resolves_to_org_default_when_tier_has_no_override(
    organization: Organization, tier: MembershipTier, org_questionnaire_a: OrganizationQuestionnaire
) -> None:
    organization.default_membership_questionnaire = org_questionnaire_a
    organization.save(update_fields=["default_membership_questionnaire"])
    assert resolve_membership_questionnaire(organization, tier) == org_questionnaire_a


def test_questionnaire_tier_override_beats_org_default(
    organization: Organization,
    tier: MembershipTier,
    org_questionnaire_a: OrganizationQuestionnaire,
    org_questionnaire_b: OrganizationQuestionnaire,
) -> None:
    organization.default_membership_questionnaire = org_questionnaire_a
    organization.save(update_fields=["default_membership_questionnaire"])
    tier.membership_questionnaire = org_questionnaire_b
    tier.save(update_fields=["membership_questionnaire"])
    assert resolve_membership_questionnaire(organization, tier) == org_questionnaire_b


def test_questionnaire_resolves_to_org_when_tier_is_none(
    organization: Organization, org_questionnaire_a: OrganizationQuestionnaire
) -> None:
    organization.default_membership_questionnaire = org_questionnaire_a
    organization.save(update_fields=["default_membership_questionnaire"])
    assert resolve_membership_questionnaire(organization, None) == org_questionnaire_a


def test_requires_approval_inherits_org_when_tier_is_null(organization: Organization, tier: MembershipTier) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    assert resolve_requires_membership_approval(organization, tier) is True


def test_requires_approval_tier_override_false_beats_org_true(organization: Organization, tier: MembershipTier) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    tier.requires_membership_approval = False
    tier.save(update_fields=["requires_membership_approval"])
    assert resolve_requires_membership_approval(organization, tier) is False


def test_requires_approval_defaults_false_for_unconfigured_org(
    organization: Organization, tier: MembershipTier
) -> None:
    assert resolve_requires_membership_approval(organization, tier) is False


def test_requires_approval_with_tier_none_uses_org(organization: Organization) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    assert resolve_requires_membership_approval(organization, None) is True
