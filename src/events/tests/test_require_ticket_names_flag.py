"""Tests for the Event.require_ticket_names flag (#845)."""

import pytest
from django.test import Client
from django.urls import reverse

from events.models import Event, Ticket

pytestmark = pytest.mark.django_db


def test_event_defaults_to_requiring_ticket_names(event: Event) -> None:
    """A freshly created event keeps today's behavior: holder names are required."""
    assert event.require_ticket_names is True


def test_blank_guest_name_passes_validation(ticket: Ticket) -> None:
    """``guest_name`` is blank-capable so name-less tickets can be stored."""
    ticket.guest_name = ""

    ticket.full_clean()  # must not raise


def test_flag_exposed_on_public_event_detail(public_event: Event) -> None:
    """The flag is part of the public event payload so clients can adapt checkout."""
    url = reverse("api:get_event", kwargs={"event_id": str(public_event.id)})

    response = Client().get(url)

    assert response.status_code == 200
    assert response.json()["require_ticket_names"] is True
