"""Tests for questionnaire submission operations.

Tests for list, detail, and evaluate submission endpoints.
"""

from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

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
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- List submissions tests ---


def test_list_submissions_success(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test that organization staff can list questionnaire submissions."""
    # Create questionnaire and org questionnaire
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create ready submissions
    submission1 = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )
    # Create another user for the second submission
    another_user = RevelUser.objects.create_user(username="another_user", email="another@example.com", password="pass")
    submission2 = QuestionnaireSubmission.objects.create(
        user=another_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    # Create draft submission (should not appear)
    QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
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
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create submissions
    submission1 = QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
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
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create users for different submissions
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")

    # Create submissions with different evaluation statuses
    submission_approved = QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission_approved,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        evaluator=organization.owner,
    )

    submission_rejected = QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission_rejected,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        evaluator=organization.owner,
    )

    submission_pending = QuestionnaireSubmission.objects.create(
        user=user3, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission_pending,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
        evaluator=organization.owner,
    )

    # Create submission without evaluation
    submission_no_eval = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
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
    # Create questionnaire
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create users
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")

    # Create submissions with different submission times
    now = timezone.now()
    submission1 = QuestionnaireSubmission.objects.create(
        user=user1, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    submission1.submitted_at = now - timedelta(days=2)
    submission1.save()

    submission2 = QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    submission2.submitted_at = now - timedelta(days=1)
    submission2.save()

    submission3 = QuestionnaireSubmission.objects.create(
        user=user3, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
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


# --- Get submission detail tests ---


def test_get_submission_detail_success(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test getting detailed submission with answers."""
    # Create questionnaire with questions
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
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
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    MultipleChoiceAnswer.objects.create(submission=submission, question=mc_question, option=mc_option)

    FreeTextAnswer.objects.create(submission=submission, question=ft_question, answer="This is my detailed answer.")

    # Create evaluation
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
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
    assert data["user"]["email"] == member_user.email
    assert data["user"]["first_name"] == member_user.first_name
    assert data["user"]["last_name"] == member_user.last_name
    assert data["status"] == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    assert data["evaluation"]["status"] == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
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
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
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
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
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
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    url = reverse(
        "api:get_submission_detail",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id},
    )
    response = nonmember_client.get(url)

    assert response.status_code == 404


# --- Evaluate submission tests ---


def test_evaluate_submission_approve(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test approving a submission."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    payload = {
        "status": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        "score": "92.5",
        "comments": "Excellent submission!",
    }

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    assert Decimal(data["score"]) == Decimal("92.50")
    assert data["comments"] == "Excellent submission!"
    assert data["submission_id"] == str(submission.id)

    # Verify evaluation was created
    evaluation = QuestionnaireEvaluation.objects.get(submission=submission)
    assert evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    assert evaluation.score == Decimal("92.50")
    assert evaluation.evaluator == organization.owner


def test_evaluate_submission_reject(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test rejecting a submission."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    payload = {
        "status": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        "comments": "Needs improvement in several areas.",
    }

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    assert data["score"] is None
    assert data["comments"] == "Needs improvement in several areas."


def test_evaluate_submission_update_existing(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test updating an existing evaluation."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    # Create initial evaluation
    initial_evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
        score=Decimal("70.0"),
        comments="Initial review",
        evaluator=organization.owner,
    )

    payload = {
        "status": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
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
    assert data["status"] == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    assert Decimal(data["score"]) == Decimal("85.00")
    assert data["comments"] == "Updated: looks good now!"


def test_evaluate_submission_invalid_score(
    organization: Organization, organization_owner_client: Client, member_user: RevelUser
) -> None:
    """Test evaluation with invalid score returns validation error."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    payload = {
        "status": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
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
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    payload = {"status": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED, "score": "85.0"}

    url = reverse(
        "api:evaluate_submission", kwargs={"org_questionnaire_id": org_questionnaire.id, "submission_id": submission.id}
    )
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404
