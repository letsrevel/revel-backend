"""Tests for /events/{event_id}/questionnaire/{questionnaire_id} endpoints."""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from events.models import (
    Event,
    OrganizationQuestionnaire,
)
from questionnaires.models import (
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


def test_get_questionnaire_success(
    nonmember_client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test successfully retrieving a questionnaire for a visible event."""
    url = reverse(
        "api:get_questionnaire", kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk}
    )
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(event_questionnaire.pk)
    assert len(data["multiple_choice_questions"]) == 1
    assert len(data["free_text_questions"]) == 1


def test_get_questionnaire_for_invisible_event_fails(
    nonmember_client: Client, private_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test that trying to get a questionnaire for an event the user can't see returns 404."""
    # Link the questionnaire to the private event instead
    OrganizationQuestionnaire.objects.filter(questionnaire=event_questionnaire).delete()
    org_q = OrganizationQuestionnaire.objects.create(
        organization=private_event.organization, questionnaire=event_questionnaire
    )
    org_q.events.add(private_event)

    url = reverse(
        "api:get_questionnaire", kwargs={"event_id": private_event.pk, "questionnaire_id": event_questionnaire.pk}
    )
    response = nonmember_client.get(url)
    assert response.status_code == 404


def test_get_nonexistent_questionnaire_fails(nonmember_client: Client, public_event: Event) -> None:
    """Test that getting a non-existent questionnaire ID returns 404."""
    url = reverse("api:get_questionnaire", kwargs={"event_id": public_event.pk, "questionnaire_id": uuid.uuid4()})
    response = nonmember_client.get(url)
    assert response.status_code == 404


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_success_no_auto_eval(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    event_questionnaire: Questionnaire,
) -> None:
    """Test a successful submission that does not trigger immediate evaluation."""
    mcq = event_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
        "free_text_answers": [],
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert "score" not in data  # It returns a QuestionnaireSubmissionResponseSchema
    assert QuestionnaireSubmission.objects.count() == 1
    submission = QuestionnaireSubmission.objects.first()
    mock_evaluate_task.assert_called_once_with(str(submission.pk))  # type: ignore[union-attr]


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_success_with_auto_eval(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    auto_eval_questionnaire: Questionnaire,
) -> None:
    """Test a successful submission that triggers immediate evaluation."""
    mcq = auto_eval_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": auto_eval_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(auto_eval_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert "score" not in data
    assert QuestionnaireSubmission.objects.count() == 1
    submission = QuestionnaireSubmission.objects.first()
    mock_evaluate_task.assert_called_once_with(str(submission.pk))  # type: ignore[union-attr]


def test_submit_questionnaire_missing_mandatory_fails(
    nonmember_client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test submitting without a mandatory answer returns 400."""
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    # The questionnaire has one mandatory MCQ, but we submit no answers.
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert "You are missing mandatory answers" in response.json()["detail"]


def test_submit_questionnaire_anonymous_fails(
    client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test an anonymous user cannot submit a questionnaire."""
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
    }
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    # This should fail because the service layer expects a RevelUser, not AnonymousUser.
    # The generic exception handler catches this and returns a 400.
    assert response.status_code == 401


def test_submit_questionnaire_fails_after_deadline(
    nonmember_client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test submitting a questionnaire fails when application deadline has passed."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()

    mcq = event_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
        "free_text_answers": [],
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "deadline has passed" in response.json()["detail"]
    assert QuestionnaireSubmission.objects.count() == 0


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_succeeds_before_deadline(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    event_questionnaire: Questionnaire,
) -> None:
    """Test submitting a questionnaire succeeds when deadline has not passed."""
    public_event.apply_before = timezone.now() + timedelta(hours=1)
    public_event.save()

    mcq = event_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
        "free_text_answers": [],
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert QuestionnaireSubmission.objects.count() == 1


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_succeeds_without_deadline_when_event_in_future(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    event_questionnaire: Questionnaire,
) -> None:
    """Test submitting a questionnaire succeeds when no deadline is set and event is in future."""
    assert public_event.apply_before is None

    mcq = event_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
        "free_text_answers": [],
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert QuestionnaireSubmission.objects.count() == 1


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_fails_when_event_start_passed(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    event_questionnaire: Questionnaire,
) -> None:
    """Test submitting a questionnaire fails when apply_before is None but event start has passed."""
    public_event.apply_before = None
    public_event.start = timezone.now() - timedelta(hours=1)
    public_event.end = timezone.now() + timedelta(hours=23)
    public_event.save()

    mcq = event_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
        "free_text_answers": [],
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "deadline" in response.json()["detail"].lower()
