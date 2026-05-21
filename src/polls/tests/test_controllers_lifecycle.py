"""Controller tests for poll creation and lifecycle actions."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def _make_payload(organization: Organization, **overrides: t.Any) -> dict[str, t.Any]:
    payload: dict[str, t.Any] = {
        "name": "p",
        "organization_id": str(organization.id),
        "vote_visibility": "public",
        "result_visibility": "public",
        "result_timing": "after_vote",
        "staff_anonymous": True,
        "public_anonymous": True,
    }
    payload.update(overrides)
    return payload


def test_create_requires_manage_polls_permission(authenticated_client: Client, organization: Organization) -> None:
    response = authenticated_client.post(
        "/api/polls/", data=_make_payload(organization), content_type="application/json"
    )
    assert response.status_code == 403


def test_create_succeeds_for_owner(owner_client: Client, organization: Organization) -> None:
    response = owner_client.post("/api/polls/", data=_make_payload(organization), content_type="application/json")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == Poll.PollStatus.DRAFT.value


def test_create_silently_forces_no_evaluation(owner_client: Client, organization: Organization) -> None:
    response = owner_client.post("/api/polls/", data=_make_payload(organization), content_type="application/json")
    assert response.status_code == 201
    poll_id = response.json()["id"]
    poll = Poll.objects.get(pk=poll_id)
    # ``requires_evaluation`` only exists on OrganizationQuestionnaire; polls
    # bypass it by pinning their underlying questionnaire to MANUAL mode.
    assert poll.questionnaire.evaluation_mode == Questionnaire.QuestionnaireEvaluationMode.MANUAL


def test_open_transition(owner_client: Client, organization: Organization, questionnaire: Questionnaire) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    response = owner_client.post(f"/api/polls/{poll.id}/open", content_type="application/json")
    assert response.status_code == 200
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.OPEN


def test_close_transition(owner_client: Client, organization: Organization, questionnaire: Questionnaire) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )
    response = owner_client.post(f"/api/polls/{poll.id}/close", content_type="application/json")
    assert response.status_code == 200
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.CLOSED


def test_reopen_requires_future_closes_at(
    owner_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.CLOSED,
        closed_at=timezone.now(),
    )
    response = owner_client.post(
        f"/api/polls/{poll.id}/reopen",
        data={"closes_at": (timezone.now() + timedelta(hours=1)).isoformat()},
        content_type="application/json",
    )
    assert response.status_code == 200
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.OPEN


def test_delete_only_by_owner(staff_client: Client, organization: Organization, questionnaire: Questionnaire) -> None:
    """Staff with ``manage_polls`` cannot delete; only the org owner can."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    response = staff_client.delete(f"/api/polls/{poll.id}/", content_type="application/json")
    assert response.status_code == 403


def test_delete_succeeds_for_owner(
    owner_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    response = owner_client.delete(f"/api/polls/{poll.id}/", content_type="application/json")
    assert response.status_code == 204
    assert not Poll.objects.filter(pk=poll.id).exists()
