"""Tests for searching the admin ticket list by ticket holder name (#845)."""

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Ticket

pytestmark = pytest.mark.django_db


def test_admin_ticket_search_finds_holder_name(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Searching by guest_name matches the ticket holder, not just the purchaser."""
    pending_offline_ticket.guest_name = "Zaphod Beeblebrox"
    pending_offline_ticket.save(update_fields=["guest_name"])

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"search": "Zaphod"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["guest_name"] == "Zaphod Beeblebrox"


def test_admin_ticket_search_excludes_non_matching_holder(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
    pending_at_door_ticket: Ticket,
) -> None:
    """A holder-name search does not return tickets held by someone else."""
    pending_offline_ticket.guest_name = "Zaphod Beeblebrox"
    pending_offline_ticket.save(update_fields=["guest_name"])
    pending_at_door_ticket.guest_name = "Trillian Astra"
    pending_at_door_ticket.save(update_fields=["guest_name"])

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"search": "Trillian"})

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["results"]] == [str(pending_at_door_ticket.id)]
