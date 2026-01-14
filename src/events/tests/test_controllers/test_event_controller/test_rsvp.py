"""Tests for POST /events/{event_id}/rsvp/{answer} endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import (
    Event,
    EventRSVP,
    OrganizationQuestionnaire,
)
from events.service.event_manager import NextStep, Reasons
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def test_rsvp_to_event_success(nonmember_client: Client, rsvp_only_public_event: Event) -> None:
    """Test that an authenticated user can successfully RSVP to an eligible event."""
    url = reverse("api:rsvp_event", kwargs={"event_id": rsvp_only_public_event.pk, "answer": "yes"})
    response = nonmember_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "yes"
    assert data["event_id"] == str(rsvp_only_public_event.pk)

    assert EventRSVP.objects.filter(
        event=rsvp_only_public_event, user__username="nonmember_user", status="yes"
    ).exists()


def test_rsvp_to_event_requires_ticket_fails(nonmember_client: Client, public_event: Event) -> None:
    """Test that trying to RSVP to an event that requires a ticket fails with the correct error."""
    # public_event requires a ticket by default

    quest = Questionnaire.objects.create(name="Quest", min_score=100, status="published")
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        questionnaire=quest, organization=public_event.organization
    )
    org_questionnaire.events.add(public_event)
    url = reverse("api:rsvp_event", kwargs={"event_id": public_event.pk, "answer": "yes"})
    response = nonmember_client.post(url)

    assert response.status_code == 400
    data = response.json()
    assert data["allowed"] is False
    assert data["reason"] == "Requires a ticket."
    assert data["next_step"] == "purchase_ticket"


def test_rsvp_to_event_ineligible_fails(member_client: Client, members_only_event: Event) -> None:
    """Test that trying to RSVP to an event for which the user is ineligible fails."""
    members_only_event.requires_ticket = False
    members_only_event.save()

    quest = Questionnaire.objects.create(name="Quest", min_score=100, status="published")
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        questionnaire=quest, organization=members_only_event.organization
    )
    org_questionnaire.events.add(members_only_event)
    org_questionnaire.save()

    url = reverse("api:rsvp_event", kwargs={"event_id": members_only_event.pk, "answer": "yes"})
    response = member_client.post(url)

    assert response.status_code == 400
    data = response.json()
    assert data["allowed"] is False
    assert data["reason"] == Reasons.QUESTIONNAIRE_MISSING
    assert data["next_step"] == NextStep.COMPLETE_QUESTIONNAIRE


def test_rsvp_to_event_anonymous_fails(client: Client, rsvp_only_public_event: Event) -> None:
    """Test that an anonymous user cannot RSVP."""
    url = reverse("api:rsvp_event", kwargs={"event_id": rsvp_only_public_event.pk, "answer": "yes"})
    response = client.post(url)

    assert response.status_code == 401
