"""Tests for organization questionnaire core CRUD operations.

Tests for create, list, get, update, delete, and status update operations.
"""

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
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Create questionnaire tests ---


def test_create_org_questionnaire(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be created with defaults."""

    payload = OrganizationQuestionnaireCreateSchema(
        name="New Questionnaire",
        min_score=Decimal("0.0"),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
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
    assert data["questionnaire_type"] == OrganizationQuestionnaire.QuestionnaireType.ADMISSION
    assert data["max_submission_age"] is None


def test_create_org_questionnaire_with_custom_fields(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that an organization questionnaire can be created with custom type and max_submission_age."""
    payload = OrganizationQuestionnaireCreateSchema(
        name="Feedback Questionnaire",
        min_score=Decimal("50.0"),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
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
    assert data["questionnaire_type"] == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK
    assert data["max_submission_age"] == 2.5 * 3600  # 2.5 hours in seconds (float)


# --- List questionnaires tests ---


def test_list_org_questionnaires_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that organization questionnaires can be listed."""
    # Create questionnaires
    questionnaire1 = Questionnaire.objects.create(
        name="Test Questionnaire 1", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    questionnaire2 = Questionnaire.objects.create(
        name="Test Questionnaire 2", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
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
        name="Python Workshop", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    questionnaire2 = Questionnaire.objects.create(
        name="JavaScript Quiz", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
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
        name="Questionnaire 1", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
    )
    questionnaire2 = Questionnaire.objects.create(
        name="Questionnaire 2", evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL
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
        user=user1, questionnaire=questionnaire1, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )

    # 2. Submission with pending review evaluation (pending)
    submission1_pending = QuestionnaireSubmission.objects.create(
        user=user2, questionnaire=questionnaire1, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission1_pending,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
        evaluator=organization.owner,
    )

    # 3. Submission with approved evaluation (NOT pending)
    submission1_approved = QuestionnaireSubmission.objects.create(
        user=user3, questionnaire=questionnaire1, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission1_approved,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        evaluator=organization.owner,
    )

    # 4. Draft submission (should NOT be counted)
    QuestionnaireSubmission.objects.create(
        user=user4, questionnaire=questionnaire1, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT
    )

    # For questionnaire 2: Create one submission with no evaluation
    QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire2,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
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


# --- Get questionnaire tests ---


def test_get_org_questionnaire_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be retrieved."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire",
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        min_score=Decimal("75.0"),
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:get_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire"]["name"] == "Test Questionnaire"
    assert data["questionnaire"]["min_score"] == "75.00"
    assert data["questionnaire"]["evaluation_mode"] == Questionnaire.QuestionnaireEvaluationMode.MANUAL


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


# --- Update questionnaire tests ---


def test_update_org_questionnaire_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        max_submission_age=timedelta(minutes=30),
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )

    payload = {
        "max_submission_age": 3600,
        "questionnaire_type": OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    }  # 1 hour

    url = reverse("api:update_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire_type"] == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK

    # Verify questionnaire was updated
    org_questionnaire.refresh_from_db()
    assert org_questionnaire.max_submission_age == timedelta(hours=1)
    assert org_questionnaire.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK


def test_update_org_questionnaire_underlying_questionnaire(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that the underlying questionnaire fields can be updated."""
    questionnaire = Questionnaire.objects.create(
        name="Original Name",
        min_score=Decimal("50.0"),
        shuffle_questions=False,
        shuffle_sections=False,
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {
        "name": "Updated Name",
        "min_score": "75.0",
        "shuffle_questions": True,
        "shuffle_sections": True,
        "evaluation_mode": Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
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
    assert data["questionnaire"]["evaluation_mode"] == Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC

    # Verify underlying questionnaire was updated
    questionnaire.refresh_from_db()
    assert questionnaire.name == "Updated Name"
    assert questionnaire.min_score == Decimal("75.00")
    assert questionnaire.shuffle_questions is True
    assert questionnaire.shuffle_sections is True
    assert questionnaire.evaluation_mode == Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
    assert questionnaire.llm_guidelines == "Be strict in evaluation"
    assert questionnaire.can_retake_after is not None
    assert questionnaire.can_retake_after.total_seconds() == 3600
    assert questionnaire.max_attempts == 3


def test_update_org_questionnaire_partial(organization: Organization, organization_owner_client: Client) -> None:
    """Test that an organization questionnaire can be partially updated."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire",
        min_score=Decimal("50.0"),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
    )
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        max_submission_age=timedelta(minutes=30),
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )

    # Only update one field
    payload = {"llm_guidelines": "New guidelines"}

    url = reverse("api:update_org_questionnaire", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["questionnaire_type"] == OrganizationQuestionnaire.QuestionnaireType.ADMISSION  # Unchanged
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


# --- Delete questionnaire tests ---


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
