"""Tests for the questionnaire summary endpoint.

Tests aggregate statistics including status counts, score stats, and MC answer distributions.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventQuestionnaireSubmission,
    EventSeries,
    Organization,
    OrganizationQuestionnaire,
)
from questionnaires.models import (
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db

SUMMARY_URL_NAME = "api:questionnaire_summary"

# Auto-incrementing offset to guarantee distinct submitted_at timestamps across calls
_submission_counter = 0


def _create_ready_submission(
    user: RevelUser,
    questionnaire: Questionnaire,
) -> QuestionnaireSubmission:
    """Helper to create a READY submission with a distinct submitted_at timestamp."""
    global _submission_counter  # noqa: PLW0603
    _submission_counter += 1
    return QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now() + timedelta(seconds=_submission_counter),
    )


def _link_submission_to_event(
    submission: QuestionnaireSubmission,
    event: Event,
    questionnaire: Questionnaire,
    user: RevelUser,
) -> EventQuestionnaireSubmission:
    """Helper to create an EventQuestionnaireSubmission tracking record."""
    return EventQuestionnaireSubmission.objects.create(
        user=user,
        event=event,
        questionnaire=questionnaire,
        submission=submission,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )


# --- Basic summary tests ---


def test_summary_empty(
    organization: Organization,
    organization_owner_client: Client,
) -> None:
    """Summary with no submissions returns all zeros and empty MC stats."""
    questionnaire = Questionnaire.objects.create(name="Empty Q")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["total_submissions"] == 0
    assert data["unique_users"] == 0
    assert data["by_status"]["approved"] == 0
    assert data["by_status"]["rejected"] == 0
    assert data["by_status"]["pending_review"] == 0
    assert data["by_status"]["not_evaluated"] == 0
    assert data["by_status_per_user"]["approved"] == 0
    assert data["score_stats"]["avg"] is None
    assert data["score_stats"]["min"] is None
    assert data["score_stats"]["max"] is None
    assert data["mc_question_stats"] == []


def test_summary_unfiltered(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
) -> None:
    """Summary without event filter includes all READY submissions for the questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Q1")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")

    sub1 = _create_ready_submission(member_user, questionnaire)
    sub2 = _create_ready_submission(user2, questionnaire)

    # sub1 approved, sub2 rejected
    QuestionnaireEvaluation.objects.create(
        submission=sub1,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        score=Decimal("85.00"),
    )
    QuestionnaireEvaluation.objects.create(
        submission=sub2,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        score=Decimal("30.00"),
    )

    # Draft submission should be excluded
    QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
    )

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["total_submissions"] == 2
    assert data["unique_users"] == 2
    assert data["by_status"]["approved"] == 1
    assert data["by_status"]["rejected"] == 1
    assert data["by_status"]["pending_review"] == 0
    assert data["by_status"]["not_evaluated"] == 0
    assert Decimal(data["score_stats"]["avg"]) == Decimal("57.50")
    assert Decimal(data["score_stats"]["min"]) == Decimal("30.00")
    assert Decimal(data["score_stats"]["max"]) == Decimal("85.00")


# --- Event filtering ---


def test_summary_filtered_by_event(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
    event: Event,
) -> None:
    """Summary filtered by event_id only includes submissions linked to that event."""
    questionnaire = Questionnaire.objects.create(name="Q Filtered")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    sub_linked = _create_ready_submission(member_user, questionnaire)
    _link_submission_to_event(sub_linked, event, questionnaire, member_user)

    # Unlinked submission (no EventQuestionnaireSubmission)
    _create_ready_submission(
        RevelUser.objects.create_user(username="unlinked", email="unlinked@example.com", password="pass"),
        questionnaire,
    )

    QuestionnaireEvaluation.objects.create(
        submission=sub_linked,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        score=Decimal("90.00"),
    )

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url, {"event_id": str(event.id)})

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["total_submissions"] == 1
    assert data["unique_users"] == 1
    assert data["by_status"]["approved"] == 1


def test_summary_filtered_by_event_series(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
    event: Event,
    event_series: EventSeries,
) -> None:
    """Summary filtered by event_series_id includes submissions linked to events in that series."""
    questionnaire = Questionnaire.objects.create(name="Q Series")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    sub = _create_ready_submission(member_user, questionnaire)
    _link_submission_to_event(sub, event, questionnaire, member_user)

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url, {"event_series_id": str(event_series.id)})

    assert response.status_code == 200, response.content
    data = response.json()
    # event belongs to event_series (from conftest), so the submission should be included
    assert data["total_submissions"] == 1


# --- Per-user status (latest submission wins) ---


def test_summary_per_user_latest_submission_wins(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
) -> None:
    """Per-user status uses the latest submission. Rejected then approved -> per-user shows approved."""
    questionnaire = Questionnaire.objects.create(name="Q Retake")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # First submission (older) -> rejected
    sub1 = _create_ready_submission(member_user, questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=sub1,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        score=Decimal("20.00"),
    )

    # Second submission (newer) -> approved
    sub2 = _create_ready_submission(member_user, questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=sub2,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        score=Decimal("90.00"),
    )

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()

    # Total submissions count is 2
    assert data["total_submissions"] == 2
    assert data["unique_users"] == 1

    # Per-submission counts
    assert data["by_status"]["approved"] == 1
    assert data["by_status"]["rejected"] == 1

    # Per-user counts (latest wins -> approved)
    assert data["by_status_per_user"]["approved"] == 1
    assert data["by_status_per_user"]["rejected"] == 0


# --- MC answer distributions ---


def test_summary_mc_answer_distributions(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
) -> None:
    """MC question stats correctly aggregate answer counts per option."""
    questionnaire = Questionnaire.objects.create(name="Q MC")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    mc_q = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="What is 2+2?",
        order=1,
    )
    correct_opt = MultipleChoiceOption.objects.create(question=mc_q, option="4", is_correct=True, order=1)
    wrong_opt = MultipleChoiceOption.objects.create(question=mc_q, option="5", is_correct=False, order=2)
    unchosen_opt = MultipleChoiceOption.objects.create(question=mc_q, option="3", is_correct=False, order=3)

    user2 = RevelUser.objects.create_user(username="mc_user2", email="mc2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="mc_user3", email="mc3@example.com", password="pass")

    sub1 = _create_ready_submission(member_user, questionnaire)
    sub2 = _create_ready_submission(user2, questionnaire)
    sub3 = _create_ready_submission(user3, questionnaire)

    # 2 users chose correct, 1 chose wrong, nobody chose unchosen
    MultipleChoiceAnswer.objects.create(submission=sub1, question=mc_q, option=correct_opt)
    MultipleChoiceAnswer.objects.create(submission=sub2, question=mc_q, option=correct_opt)
    MultipleChoiceAnswer.objects.create(submission=sub3, question=mc_q, option=wrong_opt)

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()

    assert len(data["mc_question_stats"]) == 1
    mc_stat = data["mc_question_stats"][0]
    assert mc_stat["question_text"] == "What is 2+2?"

    options_by_id = {opt["option_id"]: opt for opt in mc_stat["options"]}
    correct_stat = options_by_id[str(correct_opt.id)]
    wrong_stat = options_by_id[str(wrong_opt.id)]
    unchosen_stat = options_by_id[str(unchosen_opt.id)]

    assert correct_stat["option_text"] == "4"
    assert correct_stat["is_correct"] is True
    assert correct_stat["count"] == 2

    assert wrong_stat["option_text"] == "5"
    assert wrong_stat["is_correct"] is False
    assert wrong_stat["count"] == 1

    assert unchosen_stat["option_text"] == "3"
    assert unchosen_stat["is_correct"] is False
    assert unchosen_stat["count"] == 0


# --- Permissions ---


def test_summary_permission_denied_for_nonmember(
    organization: Organization,
    nonmember_client: Client,
) -> None:
    """Non-member users cannot access the summary endpoint."""
    questionnaire = Questionnaire.objects.create(name="Q Denied")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = nonmember_client.get(url)

    # Should get 404 (the queryset filters out questionnaires the user can't see)
    assert response.status_code == 404, response.content


def test_summary_not_evaluated_submissions(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
) -> None:
    """Submissions without evaluations are counted as not_evaluated."""
    questionnaire = Questionnaire.objects.create(name="Q NoEval")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    _create_ready_submission(member_user, questionnaire)
    _create_ready_submission(
        RevelUser.objects.create_user(username="noeval_user", email="noeval@example.com", password="pass"),
        questionnaire,
    )

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["total_submissions"] == 2
    assert data["by_status"]["not_evaluated"] == 2
    assert data["by_status_per_user"]["not_evaluated"] == 2
    assert data["score_stats"]["avg"] is None


def test_summary_pending_review_status(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
) -> None:
    """Submissions with pending_review evaluation status are counted correctly."""
    questionnaire = Questionnaire.objects.create(name="Q Pending")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    sub = _create_ready_submission(member_user, questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=sub,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
    )

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["by_status"]["pending_review"] == 1
    assert data["by_status"]["not_evaluated"] == 0


# --- Mutual exclusivity of event_id and event_series_id ---


def test_summary_rejects_both_event_and_series_filters(
    organization: Organization,
    organization_owner_client: Client,
    event: Event,
    event_series: EventSeries,
) -> None:
    """Providing both event_id and event_series_id returns 400."""
    questionnaire = Questionnaire.objects.create(name="Q Both")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(
        url,
        {"event_id": str(event.id), "event_series_id": str(event_series.id)},
    )

    assert response.status_code == 400, response.content


# --- MC stats with multiple questions ---


def test_summary_mc_multiple_questions(
    organization: Organization,
    organization_owner_client: Client,
    member_user: RevelUser,
) -> None:
    """MC stats correctly groups options by question when multiple MC questions exist."""
    questionnaire = Questionnaire.objects.create(name="Q Multi MC")
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    mc_q1 = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Q1?", order=1)
    q1_opt_a = MultipleChoiceOption.objects.create(question=mc_q1, option="A", is_correct=True, order=1)
    q1_opt_b = MultipleChoiceOption.objects.create(question=mc_q1, option="B", is_correct=False, order=2)

    mc_q2 = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Q2?", order=2)
    q2_opt_x = MultipleChoiceOption.objects.create(question=mc_q2, option="X", is_correct=False, order=1)
    q2_opt_y = MultipleChoiceOption.objects.create(question=mc_q2, option="Y", is_correct=True, order=2)

    sub = _create_ready_submission(member_user, questionnaire)
    MultipleChoiceAnswer.objects.create(submission=sub, question=mc_q1, option=q1_opt_a)
    MultipleChoiceAnswer.objects.create(submission=sub, question=mc_q2, option=q2_opt_y)

    url = reverse(SUMMARY_URL_NAME, kwargs={"org_questionnaire_id": org_q.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200, response.content
    data = response.json()

    assert len(data["mc_question_stats"]) == 2

    # Verify ordering by question order
    assert data["mc_question_stats"][0]["question_text"] == "Q1?"
    assert data["mc_question_stats"][1]["question_text"] == "Q2?"

    # Q1: opt_a chosen (count=1), opt_b not chosen (count=0)
    q1_stats = data["mc_question_stats"][0]
    q1_opts = {opt["option_id"]: opt for opt in q1_stats["options"]}
    assert q1_opts[str(q1_opt_a.id)]["count"] == 1
    assert q1_opts[str(q1_opt_b.id)]["count"] == 0

    # Q2: opt_x not chosen (count=0), opt_y chosen (count=1)
    q2_stats = data["mc_question_stats"][1]
    q2_opts = {opt["option_id"]: opt for opt in q2_stats["options"]}
    assert q2_opts[str(q2_opt_x.id)]["count"] == 0
    assert q2_opts[str(q2_opt_y.id)]["count"] == 1
