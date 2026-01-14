"""Tests for POST /events/{event_id}/ticket/obtain endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


def test_ticket_checkout_success(nonmember_client: Client, public_event: Event, free_tier: TicketTier) -> None:
    """Test that an eligible user can successfully obtain a ticket."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    payload = {"tickets": [{"guest_name": "Test Guest"}]}
    response = nonmember_client.post(url, data=payload, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["checkout_url"] is None  # Free tier returns tickets directly
    assert len(data["tickets"]) == 1
    ticket_data = data["tickets"][0]
    assert ticket_data["status"] == "active"
    assert ticket_data["event"]["id"] == str(public_event.pk)
    assert ticket_data["tier"]["name"] == free_tier.name

    assert Ticket.objects.filter(event=public_event, user__username="nonmember_user").exists()


def test_ticket_checkout_for_member_success(member_client: Client, public_event: Event, free_tier: TicketTier) -> None:
    """Test that an eligible member user gets a ticket with the correct 'member' tier."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    payload = {"tickets": [{"guest_name": "Member Guest"}]}
    response = member_client.post(url, data=payload, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert len(data["tickets"]) == 1
    assert data["tickets"][0]["tier"]["name"] == free_tier.name

    ticket = Ticket.objects.get(event=public_event, user__username="member_user")
    assert ticket.tier
    assert ticket.tier.name


def test_ticket_checkout_for_rsvp_only_event_fails(
    nonmember_client: Client, rsvp_only_public_event: Event, free_tier: TicketTier
) -> None:
    """Test that trying to get a ticket for an RSVP-only event fails correctly."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": rsvp_only_public_event.pk, "tier_id": free_tier.pk})
    payload = {"tickets": [{"guest_name": "Test Guest"}]}
    response = nonmember_client.post(url, data=payload, content_type="application/json")

    assert response.status_code == 404  # there is no tier-event pair


def test_ticket_checkout_for_full_event_fails(
    nonmember_client: Client, public_user: RevelUser, public_event: Event, free_tier: TicketTier
) -> None:
    """Test that trying to get a ticket for a full event fails."""
    public_event.max_attendees = 1
    public_event.save()

    # First user takes the spot
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(guest_name="Test Guest", user=public_user, event=public_event, tier=tier)

    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    payload = {"tickets": [{"guest_name": "Test Guest"}]}
    response = nonmember_client.post(url, data=payload, content_type="application/json")

    assert response.status_code == 400
    data = response.json()
    assert data["allowed"] is False
    assert data["reason"] == "Event is full."
    assert data["next_step"] is None  # waitlist is closed by default


def test_ticket_checkout_anonymous_fails(client: Client, public_event: Event, free_tier: TicketTier) -> None:
    """Test that an anonymous user cannot obtain a ticket."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    payload = {"tickets": [{"guest_name": "Test Guest"}]}
    response = client.post(url, data=payload, content_type="application/json")

    assert response.status_code == 401
