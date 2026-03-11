"""Tests for requires_evaluation service-level validation."""

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventQuestionnaireSubmission, Organization, OrganizationQuestionnaire
from events.schema import OrganizationQuestionnaireUpdateSchema
from events.service import update_organization_questionnaire
from events.service.event_questionnaire_service import _validate_admission_resubmission
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


def _make_update_payload(**kwargs: object) -> OrganizationQuestionnaireUpdateSchema:
    """Build an update schema from JSON to avoid mypy complaints about required defaults."""
    return OrganizationQuestionnaireUpdateSchema.model_validate(kwargs)


@pytest.fixture
def feedback_org_questionnaire(organization: Organization) -> OrganizationQuestionnaire:
    """A feedback questionnaire with requires_evaluation=False."""
    q = Questionnaire.objects.create(name="Feedback Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        requires_evaluation=False,
    )


def test_update_feedback_to_requires_evaluation_raises(
    feedback_org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Setting requires_evaluation=True on a feedback questionnaire should raise 400."""
    payload = _make_update_payload(requires_evaluation=True)
    with pytest.raises(HttpError, match="Feedback questionnaires cannot require evaluation"):
        update_organization_questionnaire(feedback_org_questionnaire, payload)


def test_update_type_to_feedback_with_requires_evaluation_raises(
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Changing type to feedback while requires_evaluation=True should raise 400."""
    # org_questionnaire defaults to requires_evaluation=True
    payload = _make_update_payload(
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    )
    with pytest.raises(HttpError, match="Feedback questionnaires cannot require evaluation"):
        update_organization_questionnaire(org_questionnaire, payload)


def test_update_type_to_feedback_with_requires_evaluation_false_succeeds(
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Changing type to feedback AND requires_evaluation=False together should succeed."""
    payload = _make_update_payload(
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        requires_evaluation=False,
    )
    result = update_organization_questionnaire(org_questionnaire, payload)
    assert result.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK  # type: ignore[attr-defined]
    assert result.requires_evaluation is False  # type: ignore[attr-defined]


def test_update_admission_toggle_requires_evaluation_back_to_true(
    organization: Organization,
) -> None:
    """Toggling requires_evaluation back to True on an admission questionnaire should succeed."""
    q = Questionnaire.objects.create(name="Info Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        requires_evaluation=False,
    )
    payload = _make_update_payload(requires_evaluation=True)
    result = update_organization_questionnaire(oq, payload)
    assert result.requires_evaluation is True  # type: ignore[attr-defined]


# --- Resubmission blocking when requires_evaluation=False ---


def test_resubmission_blocked_when_no_evaluation_required(
    member_user: RevelUser,
    public_event: Event,
    organization: Organization,
) -> None:
    """Resubmitting a requires_evaluation=False questionnaire should raise 400."""
    q = Questionnaire.objects.create(name="Info Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        requires_evaluation=False,
    )
    submission = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )
    EventQuestionnaireSubmission.objects.create(
        event=public_event,
        user=member_user,
        questionnaire=q,
        submission=submission,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )

    with pytest.raises(HttpError, match="You have already submitted this questionnaire"):
        _validate_admission_resubmission(user=member_user, event=public_event, org_questionnaire=oq)
