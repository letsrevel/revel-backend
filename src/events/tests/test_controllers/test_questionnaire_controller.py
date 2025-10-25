"""test_questionnaire_controller.py: Unit tests for the QuestionnaireController."""

from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, OrganizationQuestionnaire
from events.schema import OrganizationQuestionnaireCreateSchema
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
    SectionCreateSchema,
    SectionUpdateSchema,
)

pytestmark = pytest.mark.django_db


def test_create_org_questionnaire(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be created with defaults."""

    payload = OrganizationQuestionnaireCreateSchema(
        name="New Questionnaire",
        min_score=Decimal("0.0"),
        evaluation_mode=Questionnaire.EvaluationMode.AUTOMATIC,
        # Using defaults: questionnaire_type=ADMISSION, max_submission_age=None
    )
    response = organization_owner_client.post(
        reverse("api:create_questionnaire", kwargs={"organization_id": organization.id}),
        data=payload.model_dump_json(),
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire"]["name"] == "New Questionnaire"
    assert data["questionnaire_type"] == OrganizationQuestionnaire.Types.ADMISSION
    assert data["max_submission_age"] is None


def test_create_org_questionnaire_with_custom_fields(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that an organization questionnaire can be created with custom type and max_submission_age."""
    payload = OrganizationQuestionnaireCreateSchema(
        name="Feedback Questionnaire",
        min_score=Decimal("50.0"),
        evaluation_mode=Questionnaire.EvaluationMode.MANUAL,
        questionnaire_type=OrganizationQuestionnaire.Types.FEEDBACK,
        max_submission_age=timedelta(hours=2, minutes=30),  # 2.5 hours
    )
    response = organization_owner_client.post(
        reverse("api:create_questionnaire", kwargs={"organization_id": organization.id}),
        data=payload.model_dump_json(),
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire"]["name"] == "Feedback Questionnaire"
    assert data["questionnaire_type"] == OrganizationQuestionnaire.Types.FEEDBACK
    assert data["max_submission_age"] == 2.5 * 3600  # 2.5 hours in seconds (float)


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


def test_list_submissions_filter_by_evaluation_status(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test that submissions can be filtered by evaluation status."""
    # Create questionnaire
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create users for different submissions
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")

    # Create submissions with different evaluation statuses
    submission_approved = QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission_approved,
        status=QuestionnaireEvaluation.Status.APPROVED,
        evaluator=organization.owner,
    )

    submission_rejected = QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission_rejected,
        status=QuestionnaireEvaluation.Status.REJECTED,
        evaluator=organization.owner,
    )

    submission_pending = QuestionnaireSubmission.objects.create(
        user=user3, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission_pending,
        status=QuestionnaireEvaluation.Status.PENDING_REVIEW,
        evaluator=organization.owner,
    )

    # Create submission without evaluation
    submission_no_eval = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    url = reverse("api:list_submissions", kwargs={"org_questionnaire_id": org_questionnaire.id})

    # Test filter by approved
    response = organization_owner_client.get(url, {"evaluation_status": "approved"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(submission_approved.id)

    # Test filter by rejected
    response = organization_owner_client.get(url, {"evaluation_status": "rejected"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(submission_rejected.id)

    # Test filter by pending review
    response = organization_owner_client.get(url, {"evaluation_status": "pending review"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(submission_pending.id)

    # Test filter by no_evaluation
    response = organization_owner_client.get(url, {"evaluation_status": "no_evaluation"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(submission_no_eval.id)

    # Test no filter (should return all)
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 4


def test_list_submissions_ordering(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test that submissions can be ordered by submission time."""
    from datetime import timedelta

    from django.utils import timezone

    # Create questionnaire
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create users
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")

    # Create submissions with different submission times
    now = timezone.now()
    submission1 = QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    submission1.submitted_at = now - timedelta(days=2)
    submission1.save()

    submission2 = QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    submission2.submitted_at = now - timedelta(days=1)
    submission2.save()

    submission3 = QuestionnaireSubmission.objects.create(
        user=user3, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    submission3.submitted_at = now
    submission3.save()

    url = reverse("api:list_submissions", kwargs={"org_questionnaire_id": org_questionnaire.id})

    # Test default ordering (-submitted_at, newest first)
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    assert data["results"][0]["id"] == str(submission3.id)  # Newest
    assert data["results"][1]["id"] == str(submission2.id)
    assert data["results"][2]["id"] == str(submission1.id)  # Oldest

    # Test ordering by submitted_at (oldest first)
    response = organization_owner_client.get(url, {"order_by": "submitted_at"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    assert data["results"][0]["id"] == str(submission1.id)  # Oldest
    assert data["results"][1]["id"] == str(submission2.id)
    assert data["results"][2]["id"] == str(submission3.id)  # Newest

    # Test ordering by -submitted_at (newest first, explicitly)
    response = organization_owner_client.get(url, {"order_by": "-submitted_at"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    assert data["results"][0]["id"] == str(submission3.id)  # Newest
    assert data["results"][1]["id"] == str(submission2.id)
    assert data["results"][2]["id"] == str(submission1.id)  # Oldest


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
    assert isinstance(mc_answer_data["answer_content"], list)
    assert len(mc_answer_data["answer_content"]) == 1
    assert mc_answer_data["answer_content"][0]["option_id"] == str(mc_option.id)
    assert mc_answer_data["answer_content"][0]["option_text"] == "Blue"
    assert mc_answer_data["answer_content"][0]["is_correct"] is True

    ft_answer_data = next(a for a in data["answers"] if a["question_type"] == "free_text")
    assert ft_answer_data["question_id"] == str(ft_question.id)
    assert isinstance(ft_answer_data["answer_content"], list)
    assert len(ft_answer_data["answer_content"]) == 1
    assert ft_answer_data["answer_content"][0]["answer"] == "This is my detailed answer."
    assert "is_correct" not in ft_answer_data["answer_content"][0]


def test_get_submission_detail_with_multiple_selections(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test getting detailed submission with multiple answers to a single question."""
    # Create questionnaire with a multiple-selection question
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create a multiple choice question that allows multiple answers
    mc_question = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Which colors do you like?", order=1, allow_multiple_answers=True
    )
    mc_option_1 = MultipleChoiceOption.objects.create(question=mc_question, option="Blue", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mc_question, option="Red", is_correct=True, order=2)
    mc_option_3 = MultipleChoiceOption.objects.create(question=mc_question, option="Green", is_correct=False, order=3)

    # Create submission with multiple answers to the same question
    submission = QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )

    MultipleChoiceAnswer.objects.create(submission=submission, question=mc_question, option=mc_option_1)
    MultipleChoiceAnswer.objects.create(submission=submission, question=mc_question, option=mc_option_3)

    url = reverse(
        "api:get_submission_detail",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id},
    )
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Check answers - should have ONE answer object for the question with TWO options
    assert len(data["answers"]) == 1

    mc_answer_data = data["answers"][0]
    assert mc_answer_data["question_id"] == str(mc_question.id)
    assert mc_answer_data["question_text"] == "Which colors do you like?"
    assert mc_answer_data["question_type"] == "multiple_choice"
    assert isinstance(mc_answer_data["answer_content"], list)
    assert len(mc_answer_data["answer_content"]) == 2

    # Check that both selected options are present
    option_ids = {opt["option_id"] for opt in mc_answer_data["answer_content"]}
    assert str(mc_option_1.id) in option_ids
    assert str(mc_option_3.id) in option_ids

    # Check is_correct flags
    blue_option = next(opt for opt in mc_answer_data["answer_content"] if opt["option_text"] == "Blue")
    assert blue_option["is_correct"] is True

    green_option = next(opt for opt in mc_answer_data["answer_content"] if opt["option_text"] == "Green")
    assert green_option["is_correct"] is False


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


def test_list_org_questionnaires_filter_by_event(
    organization: Organization, organization_owner_client: Client, event: Event, public_event: Event
) -> None:
    """Test that organization questionnaires can be filtered by event_id."""
    # Create questionnaires
    questionnaire1 = Questionnaire.objects.create(name="Event Questionnaire 1")
    questionnaire2 = Questionnaire.objects.create(name="Event Questionnaire 2")
    questionnaire3 = Questionnaire.objects.create(name="Unrelated Questionnaire")

    org_q1 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    org_q2 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)
    org_q3 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire3)

    # Assign questionnaires to events
    org_q1.events.add(event)
    org_q2.events.add(event, public_event)
    # org_q3 is not assigned to any event

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url, {"event_id": str(event.id)})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    questionnaire_ids = {item["id"] for item in data["results"]}
    assert str(org_q1.id) in questionnaire_ids
    assert str(org_q2.id) in questionnaire_ids
    assert str(org_q3.id) not in questionnaire_ids


def test_list_org_questionnaires_filter_by_event_series(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that organization questionnaires can be filtered by event_series_id."""
    # Create another event series
    other_series = EventSeries.objects.create(organization=organization, name="Other Series", slug="other-series")

    # Create questionnaires
    questionnaire1 = Questionnaire.objects.create(name="Series Questionnaire 1")
    questionnaire2 = Questionnaire.objects.create(name="Series Questionnaire 2")
    questionnaire3 = Questionnaire.objects.create(name="Unrelated Questionnaire")

    org_q1 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    org_q2 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)
    org_q3 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire3)

    # Assign questionnaires to event series
    org_q1.event_series.add(event_series)
    org_q2.event_series.add(event_series, other_series)
    # org_q3 is not assigned to any event series

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url, {"event_series_id": str(event_series.id)})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    questionnaire_ids = {item["id"] for item in data["results"]}
    assert str(org_q1.id) in questionnaire_ids
    assert str(org_q2.id) in questionnaire_ids
    assert str(org_q3.id) not in questionnaire_ids


def test_list_org_questionnaires_filter_by_both_event_and_series(
    organization: Organization, organization_owner_client: Client, event: Event, event_series: EventSeries
) -> None:
    """Test filtering by both event_id and event_series_id returns intersection."""
    # Create questionnaires
    questionnaire1 = Questionnaire.objects.create(name="Both Questionnaire")
    questionnaire2 = Questionnaire.objects.create(name="Event Only Questionnaire")
    questionnaire3 = Questionnaire.objects.create(name="Series Only Questionnaire")

    org_q1 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    org_q2 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)
    org_q3 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire3)

    # Assign questionnaires
    org_q1.events.add(event)
    org_q1.event_series.add(event_series)
    org_q2.events.add(event)
    org_q3.event_series.add(event_series)

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url, {"event_id": str(event.id), "event_series_id": str(event_series.id)})

    assert response.status_code == 200
    data = response.json()
    # Only org_q1 matches both filters
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(org_q1.id)


def test_list_org_questionnaires_filter_no_results(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that filtering with non-existent IDs returns empty results."""
    from uuid import uuid4

    # Create a questionnaire without any event assignment
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url, {"event_id": str(uuid4())})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0


def test_list_org_questionnaires_filter_combined_with_search(
    organization: Organization, organization_owner_client: Client, event: Event
) -> None:
    """Test that filters work correctly when combined with search."""
    # Create questionnaires
    questionnaire1 = Questionnaire.objects.create(name="Python Workshop Questionnaire")
    questionnaire2 = Questionnaire.objects.create(name="JavaScript Quiz Questionnaire")
    questionnaire3 = Questionnaire.objects.create(name="Python Advanced Questionnaire")

    org_q1 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    org_q2 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)
    org_q3 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire3)

    # Assign to event
    org_q1.events.add(event)
    org_q2.events.add(event)
    org_q3.events.add(event)

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url, {"event_id": str(event.id), "search": "Python"})

    assert response.status_code == 200
    data = response.json()
    # Should return only Python questionnaires assigned to the event
    assert data["count"] == 2

    questionnaire_names = {item["questionnaire"]["name"] for item in data["results"]}
    assert "Python Workshop Questionnaire" in questionnaire_names
    assert "Python Advanced Questionnaire" in questionnaire_names
    assert "JavaScript Quiz Questionnaire" not in questionnaire_names


def test_list_org_questionnaires_with_pending_evaluations_count(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test that pending evaluations count is correctly included in the response."""
    # Create two questionnaires
    questionnaire1 = Questionnaire.objects.create(
        name="Questionnaire 1", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    questionnaire2 = Questionnaire.objects.create(
        name="Questionnaire 2", evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_q1 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire1)
    org_q2 = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire2)

    # Create users for submissions
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")
    user4 = RevelUser.objects.create_user(username="user4", email="user4@example.com", password="pass")

    # For questionnaire 1: Create submissions with different evaluation statuses
    # 1. Submission with no evaluation (pending)
    QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire1, status=QuestionnaireSubmission.Status.READY
    )

    # 2. Submission with pending review evaluation (pending)
    submission1_pending = QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire1, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission1_pending,
        status=QuestionnaireEvaluation.Status.PENDING_REVIEW,
        evaluator=organization.owner,
    )

    # 3. Submission with approved evaluation (NOT pending)
    submission1_approved = QuestionnaireSubmission.objects.create(
        user=user3, questionnaire=questionnaire1, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission1_approved,
        status=QuestionnaireEvaluation.Status.APPROVED,
        evaluator=organization.owner,
    )

    # 4. Draft submission (should NOT be counted)
    QuestionnaireSubmission.objects.create(
        user=user4, questionnaire=questionnaire1, status=QuestionnaireSubmission.Status.DRAFT
    )

    # For questionnaire 2: Create one submission with no evaluation
    QuestionnaireSubmission.objects.create(
        user=member_user, questionnaire=questionnaire2, status=QuestionnaireSubmission.Status.READY
    )

    url = reverse("api:list_org_questionnaires")
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    # Find questionnaire1 in results
    q1_result = next(item for item in data["results"] if item["id"] == str(org_q1.id))
    assert q1_result["pending_evaluations_count"] == 2  # submission1_no_eval + submission1_pending

    # Find questionnaire2 in results
    q2_result = next(item for item in data["results"] if item["id"] == str(org_q2.id))
    assert q2_result["pending_evaluations_count"] == 1  # submission2_no_eval


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


# ===== CRUD: UPDATE & DELETE TESTS =====


def test_update_org_questionnaire_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        max_submission_age=timedelta(minutes=30),
        questionnaire_type=OrganizationQuestionnaire.Types.ADMISSION,
    )

    payload = {"max_submission_age": 3600, "questionnaire_type": OrganizationQuestionnaire.Types.FEEDBACK}  # 1 hour

    url = reverse("api:update_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire_type"] == OrganizationQuestionnaire.Types.FEEDBACK

    # Verify questionnaire was updated
    org_questionnaire.refresh_from_db()
    assert org_questionnaire.max_submission_age == timedelta(hours=1)
    assert org_questionnaire.questionnaire_type == OrganizationQuestionnaire.Types.FEEDBACK


def test_update_org_questionnaire_underlying_questionnaire(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that the underlying questionnaire fields can be updated."""
    questionnaire = Questionnaire.objects.create(
        name="Original Name",
        min_score=Decimal("50.0"),
        shuffle_questions=False,
        shuffle_sections=False,
        evaluation_mode=Questionnaire.EvaluationMode.MANUAL,
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {
        "name": "Updated Name",
        "min_score": "75.0",
        "shuffle_questions": True,
        "shuffle_sections": True,
        "evaluation_mode": Questionnaire.EvaluationMode.AUTOMATIC,
        "llm_guidelines": "Be strict in evaluation",
        "can_retake_after": 3600,  # 1 hour in seconds
        "max_attempts": 3,
    }

    url = reverse("api:update_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire"]["name"] == "Updated Name"
    assert data["questionnaire"]["min_score"] == "75.00"
    assert data["questionnaire"]["shuffle_questions"] is True
    assert data["questionnaire"]["shuffle_sections"] is True
    assert data["questionnaire"]["evaluation_mode"] == Questionnaire.EvaluationMode.AUTOMATIC

    # Verify underlying questionnaire was updated
    questionnaire.refresh_from_db()
    assert questionnaire.name == "Updated Name"
    assert questionnaire.min_score == Decimal("75.00")
    assert questionnaire.shuffle_questions is True
    assert questionnaire.shuffle_sections is True
    assert questionnaire.evaluation_mode == Questionnaire.EvaluationMode.AUTOMATIC
    assert questionnaire.llm_guidelines == "Be strict in evaluation"
    assert questionnaire.can_retake_after is not None
    assert questionnaire.can_retake_after.total_seconds() == 3600
    assert questionnaire.max_attempts == 3


def test_update_org_questionnaire_partial(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be partially updated."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", min_score=Decimal("50.0"), evaluation_mode=Questionnaire.EvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        max_submission_age=timedelta(minutes=30),
        questionnaire_type=OrganizationQuestionnaire.Types.ADMISSION,
    )

    # Only update one field
    payload = {"llm_guidelines": "New guidelines"}

    url = reverse("api:update_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire_type"] == OrganizationQuestionnaire.Types.ADMISSION  # Unchanged
    assert data["questionnaire"]["name"] == "Test Questionnaire"  # Unchanged

    # Verify only llm_guidelines was updated
    questionnaire.refresh_from_db()
    assert questionnaire.llm_guidelines == "New guidelines"
    assert questionnaire.name == "Test Questionnaire"


def test_update_org_questionnaire_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot update organization questionnaires."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"max_submission_age": 60}

    url = reverse("api:update_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_delete_org_questionnaire_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:delete_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not OrganizationQuestionnaire.objects.filter(id=org_questionnaire.id).exists()


def test_delete_org_questionnaire_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent questionnaire returns 404."""
    from uuid import uuid4

    url = reverse("api:delete_org_questionnaire", kwargs={"org_questionnaire_id": uuid4()})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_org_questionnaire_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete organization questionnaires."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:delete_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.delete(url)

    assert response.status_code == 404


def test_delete_section_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a section can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    section = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Test Section", order=1)

    url = reverse("api:delete_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": section.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not QuestionnaireSection.objects.filter(id=section.id).exists()


def test_delete_section_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent section returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:delete_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": uuid4()})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_section_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete sections."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    section = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Test Section", order=1)

    url = reverse("api:delete_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": section.id})
    response = nonmember_client.delete(url)

    assert response.status_code == 404


def test_delete_mc_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice question can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    url = reverse(
        "api:delete_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not MultipleChoiceQuestion.objects.filter(id=question.id).exists()


def test_delete_mc_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent MC question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:delete_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_mc_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete MC questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    url = reverse(
        "api:delete_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404


def test_delete_mc_option_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice option can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)
    option = MultipleChoiceOption.objects.create(question=question, option="Test Option", is_correct=True, order=1)

    url = reverse("api:delete_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": option.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not MultipleChoiceOption.objects.filter(id=option.id).exists()


def test_delete_mc_option_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent MC option returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:delete_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": uuid4()})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_mc_option_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete MC options."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)
    option = MultipleChoiceOption.objects.create(question=question, option="Test Option", is_correct=True, order=1)

    url = reverse("api:delete_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": option.id})
    response = nonmember_client.delete(url)

    assert response.status_code == 404


def test_delete_ft_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a free text question can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Test Question", order=1, positive_weight=Decimal("1.0")
    )

    url = reverse(
        "api:delete_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not FreeTextQuestion.objects.filter(id=question.id).exists()


def test_delete_ft_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent FT question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:delete_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_ft_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete FT questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Test Question", order=1, positive_weight=Decimal("1.0")
    )

    url = reverse(
        "api:delete_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404


# ===== EVENT ASSIGNMENT TESTS =====


def test_replace_events_success(
    organization: Organization, organization_owner_client: Client, event: Event, public_event: Event
) -> None:
    """Test that events can be batch replaced for a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Initially assign one event
    org_questionnaire.events.add(event)

    # Replace with two events
    payload = {"event_ids": [str(event.id), str(public_event.id)]}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200

    # Verify events were replaced
    org_questionnaire.refresh_from_db()
    event_ids = list(org_questionnaire.events.values_list("id", flat=True))
    assert len(event_ids) == 2
    assert event.id in event_ids
    assert public_event.id in event_ids


def test_replace_events_invalid_event(organization: Organization, organization_owner_client: Client) -> None:
    """Test that replacing events with invalid event ID returns 400."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_ids": [str(uuid4())]}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_events_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that events from another organization cannot be assigned."""
    from django.utils import timezone

    # Create another organization with an event
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_event = Event.objects.create(
        organization=other_org,
        name="Other Event",
        slug="other-event",
        event_type=Event.Types.PUBLIC,
        status="open",
        start=timezone.now(),
        end=timezone.now() + timedelta(hours=2),
    )

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_ids": [str(other_event.id)]}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_events_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot replace events."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload: dict[str, list[str]] = {"event_ids": []}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_assign_event_success(organization: Organization, organization_owner_client: Client, event: Event) -> None:
    """Test that a single event can be assigned to a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200

    # Verify event was assigned
    org_questionnaire.refresh_from_db()
    assert event in org_questionnaire.events.all()


def test_assign_event_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that event from another organization cannot be assigned."""
    from django.utils import timezone

    # Create another organization with an event
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_event = Event.objects.create(
        organization=other_org,
        name="Other Event",
        slug="other-event",
        event_type=Event.Types.PUBLIC,
        status="open",
        start=timezone.now(),
        end=timezone.now() + timedelta(hours=2),
    )

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": other_event.id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_assign_event_permission_denied(organization: Organization, nonmember_client: Client, event: Event) -> None:
    """Test that non-members cannot assign events."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = nonmember_client.post(url)

    assert response.status_code == 404


def test_unassign_event_success(organization: Organization, organization_owner_client: Client, event: Event) -> None:
    """Test that a single event can be unassigned from a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.events.add(event)

    url = reverse(
        "api:unassign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204

    # Verify event was unassigned
    org_questionnaire.refresh_from_db()
    assert event not in org_questionnaire.events.all()


def test_unassign_event_permission_denied(organization: Organization, nonmember_client: Client, event: Event) -> None:
    """Test that non-members cannot unassign events."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.events.add(event)

    url = reverse(
        "api:unassign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404


# ===== EVENT SERIES ASSIGNMENT TESTS =====


def test_replace_event_series_success(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that event series can be batch replaced for a questionnaire."""
    # Create another event series
    other_series = EventSeries.objects.create(organization=organization, name="Other Series", slug="other-series")

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Initially assign one series
    org_questionnaire.event_series.add(event_series)

    # Replace with two series
    payload = {"event_series_ids": [str(event_series.id), str(other_series.id)]}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200

    # Verify series were replaced
    org_questionnaire.refresh_from_db()
    series_ids = list(org_questionnaire.event_series.values_list("id", flat=True))
    assert len(series_ids) == 2
    assert event_series.id in series_ids
    assert other_series.id in series_ids


def test_replace_event_series_invalid_series(organization: Organization, organization_owner_client: Client) -> None:
    """Test that replacing event series with invalid ID returns 400."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_series_ids": [str(uuid4())]}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_event_series_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that event series from another organization cannot be assigned."""
    # Create another organization with a series
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_series = EventSeries.objects.create(organization=other_org, name="Other Series", slug="other-series")

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_series_ids": [str(other_series.id)]}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_event_series_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot replace event series."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload: dict[str, list[str]] = {"event_series_ids": []}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_assign_event_series_success(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that a single event series can be assigned to a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200

    # Verify series was assigned
    org_questionnaire.refresh_from_db()
    assert event_series in org_questionnaire.event_series.all()


def test_assign_event_series_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that event series from another organization cannot be assigned."""
    # Create another organization with a series
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_series = EventSeries.objects.create(organization=other_org, name="Other Series", slug="other-series")

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": other_series.id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_assign_event_series_permission_denied(
    organization: Organization, nonmember_client: Client, event_series: EventSeries
) -> None:
    """Test that non-members cannot assign event series."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = nonmember_client.post(url)

    assert response.status_code == 404


def test_unassign_event_series_success(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that a single event series can be unassigned from a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.event_series.add(event_series)

    url = reverse(
        "api:unassign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204

    # Verify series was unassigned
    org_questionnaire.refresh_from_db()
    assert event_series not in org_questionnaire.event_series.all()


def test_unassign_event_series_permission_denied(
    organization: Organization, nonmember_client: Client, event_series: EventSeries
) -> None:
    """Test that non-members cannot unassign event series."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.event_series.add(event_series)

    url = reverse(
        "api:unassign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404
