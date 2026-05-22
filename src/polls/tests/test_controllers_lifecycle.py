"""Controller tests for poll creation and lifecycle actions."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.utils import timezone

from events.models.event import Event
from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def _make_payload(**overrides: t.Any) -> dict[str, t.Any]:
    payload: dict[str, t.Any] = {
        "name": "p",
        "vote_visibility": "public",
        "result_visibility": "public",
        "result_timing": "after_vote",
        "staff_anonymous": True,
        "public_anonymous": True,
    }
    payload.update(overrides)
    return payload


def _create_url(organization: Organization) -> str:
    """URL for the create-poll endpoint (organization id is a path param)."""
    return f"/api/polls/organizations/{organization.id}"


def test_create_requires_manage_polls_permission(authenticated_client: Client, organization: Organization) -> None:
    response = authenticated_client.post(
        _create_url(organization), data=_make_payload(), content_type="application/json"
    )
    assert response.status_code == 403


def test_create_succeeds_for_owner(owner_client: Client, organization: Organization) -> None:
    response = owner_client.post(_create_url(organization), data=_make_payload(), content_type="application/json")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == Poll.PollStatus.DRAFT.value


def test_create_silently_forces_no_evaluation(owner_client: Client, organization: Organization) -> None:
    response = owner_client.post(_create_url(organization), data=_make_payload(), content_type="application/json")
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


# --- Finding 1: PATCH-time cross-field constraint violations ---


def test_patch_poll_event_id_null_with_private_visibility_returns_422(
    owner_client: Client, organization: Organization, event: Event, questionnaire: Questionnaire
) -> None:
    """Schema validator rejects clearing event_id while setting PRIVATE visibility."""
    poll = Poll.objects.create(
        organization=organization,
        event=event,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    response = owner_client.patch(
        f"/api/polls/{poll.id}/",
        data={"event_id": None, "vote_visibility": "private"},
        content_type="application/json",
    )
    assert response.status_code == 422


def test_patch_poll_event_id_null_breaks_existing_private_visibility_returns_422(
    owner_client: Client, organization: Organization, event: Event, questionnaire: Questionnaire
) -> None:
    """Controller catches CheckConstraint ValidationError when payload alone looks valid.

    Poll already has PRIVATE vote_visibility tied to an event; patch only clears
    event_id. The schema validator can't see the existing visibility, so the
    failure surfaces from ``Poll.save() -> full_clean -> validate_constraints``
    and the controller translates it to 422.
    """
    poll = Poll.objects.create(
        organization=organization,
        event=event,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PRIVATE,
        status=Poll.PollStatus.DRAFT,
    )
    response = owner_client.patch(
        f"/api/polls/{poll.id}/",
        data={"event_id": None},
        content_type="application/json",
    )
    assert response.status_code == 422


def test_patch_poll_result_visibility_public_with_public_anonymous_false(
    owner_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    """Setting result_visibility=PUBLIC on a poll with public_anonymous=False is rejected.

    Anonymity flags are immutable post-create, so the only way out is to reject
    the visibility change. The CheckConstraint
    ``poll_public_results_must_be_anonymous`` fires; controller returns 422.
    """
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        public_anonymous=False,
        status=Poll.PollStatus.DRAFT,
    )
    response = owner_client.patch(
        f"/api/polls/{poll.id}/",
        data={"result_visibility": "public"},
        content_type="application/json",
    )
    assert response.status_code == 422


# --- Staff (non-owner) positive write paths ---


def test_staff_with_manage_polls_can_open(
    staff_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    """Staff with ``manage_polls`` can transition a DRAFT poll to OPEN."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    response = staff_client.post(f"/api/polls/{poll.id}/open", content_type="application/json")
    assert response.status_code == 200
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.OPEN


def test_staff_with_manage_polls_can_close(
    staff_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    """Staff with ``manage_polls`` can transition an OPEN poll to CLOSED."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )
    response = staff_client.post(f"/api/polls/{poll.id}/close", content_type="application/json")
    assert response.status_code == 200
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.CLOSED


def test_staff_with_manage_polls_can_create(staff_client: Client, organization: Organization) -> None:
    """Staff with ``manage_polls`` can create a poll."""
    response = staff_client.post(_create_url(organization), data=_make_payload(), content_type="application/json")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == Poll.PollStatus.DRAFT.value
