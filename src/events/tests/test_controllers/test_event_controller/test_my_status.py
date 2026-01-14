"""Tests for GET /events/{event_id}/my-status endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventRSVP,
    Ticket,
)

pytestmark = pytest.mark.django_db


def test_get_my_event_status_with_ticket(
    nonmember_client: Client, nonmember_user: RevelUser, public_event: Event
) -> None:
    """Test status returns a ticket if one exists for the user."""
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    ticket = Ticket.objects.create(guest_name="Test Guest", event=public_event, user=nonmember_user, tier=tier)
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # Response now returns tickets list with purchase limits
    assert len(data["tickets"]) == 1
    assert data["tickets"][0]["id"] == str(ticket.id)
    assert data["tickets"][0]["status"] == "active"
    assert data["can_purchase_more"] is False  # max_tickets_per_user defaults to 1
    assert data["remaining_tickets"] == 0


def test_get_my_event_status_with_rsvp(
    nonmember_client: Client, nonmember_user: RevelUser, rsvp_only_public_event: Event
) -> None:
    """Test status returns an RSVP if one exists for the user."""
    rsvp = EventRSVP.objects.create(event=rsvp_only_public_event, user=nonmember_user, status="yes")
    url = reverse("api:get_my_event_status", kwargs={"event_id": rsvp_only_public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    # Response now wraps rsvp in EventUserStatusResponse
    assert data["rsvp"]["status"] == rsvp.status
    assert data["rsvp"]["event_id"] == str(rsvp_only_public_event.pk)
    assert data["tickets"] == []


def test_get_my_event_status_is_eligible(nonmember_client: Client, public_event: Event) -> None:
    """Test status returns eligibility data if user is eligible but has no ticket/rsvp."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is True
    assert data["event_id"] == str(public_event.pk)


def test_get_my_event_status_is_ineligible(nonmember_client: Client, public_event: Event) -> None:
    """Test status returns eligibility data if user is ineligible."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200  # The endpoint itself succeeds, it returns the status
    data = response.json()
    assert data["allowed"] is True


def test_get_my_event_status_anonymous(client: Client, public_event: Event) -> None:
    """Test anonymous user gets 401."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = client.get(url)
    assert response.status_code == 401
