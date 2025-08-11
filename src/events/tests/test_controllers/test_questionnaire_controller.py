"""test_questionnaire_controller.py: Unit tests for the QuestionnaireController."""

from decimal import Decimal

import orjson
import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization, OrganizationQuestionnaire
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSection,
    QuestionnaireSubmission,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    FreeTextQuestionUpdateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceOptionUpdateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionUpdateSchema,
    QuestionnaireCreateSchema,
    SectionCreateSchema,
    SectionUpdateSchema,
)

pytestmark = pytest.mark.django_db


def test_create_org_questionnaire(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be created."""
    payload = QuestionnaireCreateSchema(
        name="New Questionnaire",
        min_score=Decimal("0.0"),
        evaluation_mode=Questionnaire.EvaluationMode.AUTOMATIC,
    )
    response = organization_owner_client.post(
        reverse("api:create_questionnaire", kwargs={"organization_id": organization.id}),
        data=payload.model_dump_json(),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["questionnaire"]["name"] == "New Questionnaire"


def test_list_submissions_success(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test that organization staff can list questionnaire submissions."""
    # Create questionnaire and org questionnaire
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create ready submissions
    submission1 = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    # Create another user for the second submission
    another_user = RevelUser.objects.create_user(username="another_user", email="another@example.com", password="pass")
    submission2 = QuestionnaireSubmission.objects.create(
        user=another_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    # Create draft submission (should not appear)
    QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.DRAFT
    )

    url = reverse("api:list_submissions", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["count"] == 2

    submission_ids = [item["id"] for item in data["results"]]
    assert str(submission1.id) in submission_ids
    assert str(submission2.id) in submission_ids


def test_list_submissions_with_search(organization: Organization, organization_owner_client: Client) -> None:
    """Test that submissions can be searched by user email."""
    # Create users
    user1 = RevelUser.objects.create_user(
        username="user1", email="alice@example.com", password="pass", first_name="Alice"
    )
    user2 = RevelUser.objects.create_user(username="user2", email="bob@example.com", password="pass")

    # Create questionnaire
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create submissions
    submission1 = QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    url = reverse("api:list_submissions", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.get(url, {"search": "alice"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(submission1.id)


def test_list_submissions_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot list submissions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:list_submissions", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.get(url)

    assert response.status_code == 404


def test_get_submission_detail_success(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test getting detailed submission with answers."""
    # Create questionnaire with questions
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    mc_question = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="What is your favorite color?", order=1
    )
    mc_option = MultipleChoiceOption.objects.create(question=mc_question, option="Blue", is_correct=True, order=1)

    ft_question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Explain your reasoning.", order=2
    )

    # Create submission with answers
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    MultipleChoiceAnswer.objects.create(submission=submission, question=mc_question, option=mc_option)

    FreeTextAnswer.objects.create(submission=submission, question=ft_question, answer="This is my detailed answer.")

    # Create evaluation
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.Status.APPROVED,
        score=Decimal("85.0"),
        comments="Good work",
        evaluator=organization.owner,
    )

    url = reverse(
        "api:get_submission_detail",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id},
    )
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()

    assert data["id"] == str(submission.id)
    assert data["user_email"] == member_user.email
    assert data["status"] == QuestionnaireSubmission.Status.READY
    assert data["evaluation"]["status"] == QuestionnaireEvaluation.Status.APPROVED
    assert data["evaluation"]["score"] == "85.00"

    # Check answers
    assert len(data["answers"]) == 2

    mc_answer_data = next(a for a in data["answers"] if a["question_type"] == "multiple_choice")
    assert mc_answer_data["question_id"] == str(mc_question.id)
    assert mc_answer_data["answer_content"]["option_id"] == str(mc_option.id)
    assert mc_answer_data["answer_content"]["option_text"] == "Blue"

    ft_answer_data = next(a for a in data["answers"] if a["question_type"] == "free_text")
    assert ft_answer_data["question_id"] == str(ft_question.id)
    assert ft_answer_data["answer_content"]["answer"] == "This is my detailed answer."


def test_get_submission_detail_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test getting submission detail for non-existent submission."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:get_submission_detail", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": uuid4()}
    )
    response = organization_owner_client.get(url)

    assert response.status_code == 404


def test_get_submission_detail_permission_denied(
    organization: Organization, nonmember_client: Client, member_user: RevelUser
) -> None:
    """Test that non-members cannot get submission detail."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    url = reverse(
        "api:get_submission_detail",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id},
    )
    response = nonmember_client.get(url)

    assert response.status_code == 404


def test_evaluate_submission_approve(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test approving a submission."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    payload = {"status": QuestionnaireEvaluation.Status.APPROVED, "score": "92.5", "comments": "Excellent submission!"}

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == QuestionnaireEvaluation.Status.APPROVED
    assert Decimal(data["score"]) == Decimal("92.50")
    assert data["comments"] == "Excellent submission!"
    assert data["submission_id"] == str(submission.id)

    # Verify evaluation was created
    evaluation = QuestionnaireEvaluation.objects.get(submission=submission)
    assert evaluation.status == QuestionnaireEvaluation.Status.APPROVED
    assert evaluation.score == Decimal("92.50")
    assert evaluation.evaluator == organization.owner


def test_evaluate_submission_reject(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test rejecting a submission."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    payload = {"status": QuestionnaireEvaluation.Status.REJECTED, "comments": "Needs improvement in several areas."}

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == QuestionnaireEvaluation.Status.REJECTED
    assert data["score"] is None
    assert data["comments"] == "Needs improvement in several areas."


def test_evaluate_submission_update_existing(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test updating an existing evaluation."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    # Create initial evaluation
    initial_evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.Status.PENDING_REVIEW,
        score=Decimal("70.0"),
        comments="Initial review",
        evaluator=organization.owner,
    )

    payload = {
        "status": QuestionnaireEvaluation.Status.APPROVED,
        "score": "85.0",
        "comments": "Updated: looks good now!",
    }

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()

    # Should be same evaluation ID, just updated
    assert data["id"] == str(initial_evaluation.id)
    assert data["status"] == QuestionnaireEvaluation.Status.APPROVED
    assert Decimal(data["score"]) == Decimal("85.00")
    assert data["comments"] == "Updated: looks good now!"


def test_evaluate_submission_invalid_score(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test evaluation with invalid score returns validation error."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    payload = {
        "status": QuestionnaireEvaluation.Status.APPROVED,
        "score": "150.0",  # Invalid: > 100
    }

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 422


def test_evaluate_submission_permission_denied(
    organization: Organization, nonmember_client: Client, member_user: RevelUser
) -> None:
    """Test that non-members cannot evaluate submissions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    payload = {"status": QuestionnaireEvaluation.Status.APPROVED, "score": "85.0"}

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


# --- Test for GET / (list_org_questionnaires) ---


def test_list_org_questionnaires_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that organization questionnaires can be listed."""
    # Create questionnaires
    questionnaire1 = Questionnaire.objects.create(
        name="Test Questionnaire 1", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    questionnaire2 = Questionnaire.objects.create(
        name="Test Questionnaire 2", evaluation_mode=Questionnaire.EvaluationMode.AUTOMATIC
    )
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    questionnaire_names = {item["questionnaire"]["name"] for item in data["results"]}
    assert questionnaire_names == {"Test Questionnaire 1", "Test Questionnaire 2"}


def test_list_org_questionnaires_with_search(organization: Organization, organization_owner_client: Client) -> None:
    """Test that organization questionnaires can be searched by name."""
    questionnaire1 = Questionnaire.objects.create(
        name="Python Workshop", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    questionnaire2 = Questionnaire.objects.create(
        name="JavaScript Quiz", evaluation_mode=Questionnaire.EvaluationMode.AUTOMATIC
    )
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url, {"search": "Python"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["questionnaire"]["name"] == "Python Workshop"


def test_list_org_questionnaires_nonmember_access(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot list organization questionnaires."""
    url = reverse("api:list_org_questionnaires")
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0


# --- Test for GET /{org_questionnaire_id} (get_org_questionnaire) ---


def test_get_org_questionnaire_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be retrieved."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL, min_score=Decimal("75.0")
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:get_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire"]["name"] == "Test Questionnaire"
    assert data["questionnaire"]["min_score"] == "75.00"
    assert data["questionnaire"]["evaluation_mode"] == Questionnaire.EvaluationMode.MANUAL


def test_get_org_questionnaire_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that getting a non-existent org questionnaire returns 404."""
    from uuid import uuid4

    url = reverse("api:get_org_questionnaire", kwargs={"org_questionnaire_id": uuid4()})
    response = organization_owner_client.get(url)

    assert response.status_code == 404


def test_get_org_questionnaire_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot retrieve organization questionnaires."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:get_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.get(url)

    assert response.status_code == 404


# --- Test for POST /{org_questionnaire_id}/sections (create_section) ---


def test_create_section_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a section can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = SectionCreateSchema(name="New Section", order=1)

    url = reverse("api:create_section", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Section"
    assert data["order"] == 1

    # Verify section was created
    section = QuestionnaireSection.objects.get(questionnaire=questionnaire, name="New Section")
    assert section.order == 1


def test_create_section_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create sections."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = SectionCreateSchema(name="New Section", order=1)

    url = reverse("api:create_section", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Test for PUT /{org_questionnaire_id}/sections/{section_id} (update_section) ---


def test_update_section_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a section can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    section = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Original Section", order=1)

    payload = SectionUpdateSchema(name="Updated Section", order=2)

    url = reverse("api:update_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": section.id})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Section"
    assert data["order"] == 2

    # Verify section was updated
    section.refresh_from_db()
    assert section.name == "Updated Section"
    assert section.order == 2


def test_update_section_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent section returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = SectionUpdateSchema(name="Updated Section", order=2)

    url = reverse("api:update_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": uuid4()})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Test for POST /{org_questionnaire_id}/multiple-choice-questions (create_mc_question) ---


def test_create_mc_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice question can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        is_mandatory=True,
        order=1,
        allow_multiple_answers=False,
        options=[
            MultipleChoiceOptionCreateSchema(option="Red", is_correct=True, order=1),
            MultipleChoiceOptionCreateSchema(option="Blue", is_correct=False, order=2),
        ],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "What is your favorite color?"
    assert data["is_mandatory"] is True
    assert data["allow_multiple_answers"] is False
    assert len(data["options"]) == 2

    # Verify question was created
    question = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire)
    assert question.question == "What is your favorite color?"
    assert question.options.count() == 2


def test_create_mc_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create multiple choice questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        options=[MultipleChoiceOptionCreateSchema(option="Red", is_correct=True, order=1)],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Test for PUT /{org_questionnaire_id}/multiple-choice-questions/{question_id} (update_mc_question) ---


def test_update_mc_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice question can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Original Question", order=1, allow_multiple_answers=False
    )

    payload = MultipleChoiceQuestionUpdateSchema(
        question="Updated Question",
        is_mandatory=True,
        order=2,
        allow_multiple_answers=True,
        options=[
            MultipleChoiceOptionCreateSchema(option="Option A", is_correct=True, order=1),
            MultipleChoiceOptionCreateSchema(option="Option B", is_correct=False, order=2),
        ],
    )

    url = reverse(
        "api:update_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "Updated Question"
    assert data["is_mandatory"] is True
    assert data["allow_multiple_answers"] is True
    assert len(data["options"]) == 2

    # Verify question was updated
    question.refresh_from_db()
    assert question.question == "Updated Question"
    assert question.allow_multiple_answers is True


def test_update_mc_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionUpdateSchema(question="Updated Question", options=[])

    url = reverse(
        "api:update_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Test for POST /{org_questionnaire_id}/multiple-choice-options (create_mc_option) ---


def test_create_mc_option_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice option can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    payload = MultipleChoiceOptionCreateSchema(option="New Option", is_correct=True, order=1)

    url = reverse(
        "api:create_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["option"] == "New Option"
    assert data["is_correct"] is True
    assert data["order"] == 1

    # Verify option was created
    option = MultipleChoiceOption.objects.get(question=question, option="New Option")
    assert option.is_correct is True


def test_create_mc_option_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create multiple choice options."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    payload = MultipleChoiceOptionCreateSchema(option="New Option", is_correct=True, order=1)

    url = reverse(
        "api:create_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Test for PUT /{org_questionnaire_id}/multiple-choice-options/{option_id} (update_mc_option) ---


def test_update_mc_option_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice option can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)
    option = MultipleChoiceOption.objects.create(question=question, option="Original Option", is_correct=False, order=1)

    payload = MultipleChoiceOptionUpdateSchema(option="Updated Option", is_correct=True, order=2)

    url = reverse("api:update_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": option.id})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["option"] == "Updated Option"
    assert data["is_correct"] is True
    assert data["order"] == 2

    # Verify option was updated
    option.refresh_from_db()
    assert option.option == "Updated Option"
    assert option.is_correct is True


def test_update_mc_option_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent option returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceOptionUpdateSchema(option="Updated Option", is_correct=True, order=1)

    url = reverse("api:update_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": uuid4()})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Test for POST /{org_questionnaire_id}/free-text-questions (create_ft_question) ---


def test_create_ft_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a free text question can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = FreeTextQuestionCreateSchema(
        question="Explain your reasoning.",
        is_mandatory=True,
        order=1,
        positive_weight=Decimal("2.0"),
        negative_weight=Decimal("1.0"),
        is_fatal=True,
        llm_guidelines="Focus on clarity and logic.",
    )

    url = reverse("api:create_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "Explain your reasoning."
    assert data["is_mandatory"] is True
    assert data["order"] == 1
    assert Decimal(data["positive_weight"]) == Decimal("2.00")
    assert Decimal(data["negative_weight"]) == Decimal("1.00")
    assert data["is_fatal"] is True

    # Verify question was created
    question = FreeTextQuestion.objects.get(questionnaire=questionnaire)
    assert question.question == "Explain your reasoning."
    assert question.is_fatal is True


def test_create_ft_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create free text questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = FreeTextQuestionCreateSchema(question="Explain your reasoning.")

    url = reverse("api:create_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Test for PUT /{org_questionnaire_id}/free-text-questions/{question_id} (update_ft_question) ---


def test_update_ft_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a free text question can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire,
        question="Original Question",
        order=1,
        positive_weight=Decimal("1.0"),
        is_fatal=False,
    )

    payload = FreeTextQuestionUpdateSchema(
        question="Updated Question",
        is_mandatory=True,
        order=2,
        positive_weight=Decimal("3.0"),
        negative_weight=Decimal("2.0"),
        is_fatal=True,
        llm_guidelines="Updated guidelines.",
    )

    url = reverse(
        "api:update_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "Updated Question"
    assert data["is_mandatory"] is True
    assert data["order"] == 2
    assert Decimal(data["positive_weight"]) == Decimal("3.00")
    assert data["is_fatal"] is True

    # Verify question was updated
    question.refresh_from_db()
    assert question.question == "Updated Question"
    assert question.is_fatal is True


def test_update_ft_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = FreeTextQuestionUpdateSchema(question="Updated Question")

    url = reverse(
        "api:update_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404
