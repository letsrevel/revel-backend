"""Tests for blocking the retype of a membership questionnaire that is still in use (#1163)."""

import pytest
from ninja.errors import HttpError

from events.models import MembershipTier, Organization, OrganizationQuestionnaire
from events.schema import OrganizationQuestionnaireUpdateSchema
from events.service import update_organization_questionnaire
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def _make_update_payload(**kwargs: object) -> OrganizationQuestionnaireUpdateSchema:
    """Build an update schema from JSON to avoid mypy complaints about required defaults."""
    return OrganizationQuestionnaireUpdateSchema.model_validate(kwargs)


@pytest.fixture
def membership_oq(organization: Organization) -> OrganizationQuestionnaire:
    """A membership questionnaire owned by the organization."""
    q = Questionnaire.objects.create(name="Membership Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )


def test_retype_org_default_membership_questionnaire_raises(
    organization: Organization, membership_oq: OrganizationQuestionnaire
) -> None:
    """Retyping the org's default membership questionnaire is rejected and nothing is written."""
    organization.default_membership_questionnaire = membership_oq
    organization.save()
    payload = _make_update_payload(questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION, name="New")

    with pytest.raises(HttpError, match="default membership questionnaire") as exc_info:
        update_organization_questionnaire(membership_oq, payload)

    assert exc_info.value.status_code == 400
    membership_oq.refresh_from_db()
    assert membership_oq.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP
    assert membership_oq.questionnaire.name == "Membership Q"
    organization.save()  # must still validate


def test_retype_tier_membership_questionnaire_raises(
    organization: Organization, membership_oq: OrganizationQuestionnaire
) -> None:
    """Retyping a questionnaire used by a membership tier is rejected."""
    tier = MembershipTier.objects.create(organization=organization, name="Pro", membership_questionnaire=membership_oq)
    payload = _make_update_payload(questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION)

    with pytest.raises(HttpError, match="membership tiers") as exc_info:
        update_organization_questionnaire(membership_oq, payload)

    assert exc_info.value.status_code == 400
    membership_oq.refresh_from_db()
    assert membership_oq.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP
    tier.save()  # must still validate


def test_retype_unreferenced_membership_questionnaire_succeeds(membership_oq: OrganizationQuestionnaire) -> None:
    """An unreferenced membership questionnaire can be retyped."""
    payload = _make_update_payload(questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION)

    result = update_organization_questionnaire(membership_oq, payload)

    assert result.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.ADMISSION  # type: ignore[attr-defined]


def test_referenced_membership_questionnaire_other_updates_succeed(
    organization: Organization, membership_oq: OrganizationQuestionnaire
) -> None:
    """Updates that keep the MEMBERSHIP type (or omit it) are allowed while referenced."""
    organization.default_membership_questionnaire = membership_oq
    organization.save()
    payload = _make_update_payload(
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP, name="Renamed"
    )

    result = update_organization_questionnaire(membership_oq, payload)

    assert result.questionnaire.name == "Renamed"  # type: ignore[attr-defined]
