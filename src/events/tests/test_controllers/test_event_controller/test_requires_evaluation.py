"""Tests for requires_evaluation flag on questionnaire submission and creation."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import (
    Event,
    Organization,
    OrganizationQuestionnaire,
)
from events.schema import OrganizationQuestionnaireCreateSchema
from questionnaires.models import (
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Submission flow: no evaluation dispatch when requires_evaluation=False ---


@pytest.fixture
def no_eval_questionnaire(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire with requires_evaluation=False, linked to the public_event."""
    q = Questionnaire.objects.create(
        name="Info-Only Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
    )
    from questionnaires.models import MultipleChoiceOption, MultipleChoiceQuestion

    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Your dietary needs?", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="Vegan", is_correct=True)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=organization, questionnaire=q, requires_evaluation=False
    )
    org_q.events.add(public_event)
    return q


@patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
def test_submit_no_eval_questionnaire_skips_evaluation_task(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    no_eval_questionnaire: Questionnaire,
) -> None:
    """Submitting a requires_evaluation=False questionnaire should NOT dispatch evaluation task."""
    mcq = no_eval_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": no_eval_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(no_eval_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert QuestionnaireSubmission.objects.count() == 1
    mock_evaluate_task.assert_not_called()


# --- Create validation: feedback + requires_evaluation=True is rejected ---


def test_create_feedback_questionnaire_with_requires_evaluation_fails(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Creating a FEEDBACK questionnaire with requires_evaluation=True should return 400."""
    payload = OrganizationQuestionnaireCreateSchema(
        name="Bad Feedback",
        min_score=Decimal("0.0"),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        requires_evaluation=True,
    )
    response = organization_owner_client.post(
        reverse("api:create_questionnaire", kwargs={"organization_id": organization.id}),
        data=payload.model_dump_json(),
        content_type="application/json",
    )
    assert response.status_code == 400


def test_create_feedback_questionnaire_without_evaluation_succeeds(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Creating a FEEDBACK questionnaire with requires_evaluation=False should succeed."""
    payload = OrganizationQuestionnaireCreateSchema(
        name="Good Feedback",
        min_score=Decimal("0.0"),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        requires_evaluation=False,
    )
    response = organization_owner_client.post(
        reverse("api:create_questionnaire", kwargs={"organization_id": organization.id}),
        data=payload.model_dump_json(),
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["requires_evaluation"] is False


def test_create_admission_questionnaire_with_requires_evaluation_false(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Creating an ADMISSION questionnaire with requires_evaluation=False should succeed."""
    payload = OrganizationQuestionnaireCreateSchema(
        name="Info Admission",
        min_score=Decimal("0.0"),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        requires_evaluation=False,
    )
    response = organization_owner_client.post(
        reverse("api:create_questionnaire", kwargs={"organization_id": organization.id}),
        data=payload.model_dump_json(),
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["requires_evaluation"] is False
    assert data["questionnaire_type"] == "admission"
