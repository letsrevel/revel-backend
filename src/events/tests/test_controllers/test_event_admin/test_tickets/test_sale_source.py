"""Organizer-facing sale source: tells a box-office comp from a door sale (#1013)."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Ticket, TicketSaleSource, TicketTier

pytestmark = pytest.mark.django_db


def test_admin_list_exposes_sale_source(
    organization_owner_client: Client,
    event: Event,
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    tier = tier_factory(payment_method=TicketTier.PaymentMethod.AT_THE_DOOR)
    comp = ticket_factory(tier=tier, sale_source=TicketSaleSource.BOX_OFFICE_COMP)
    legacy = ticket_factory(tier=tier)

    response = organization_owner_client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))

    assert response.status_code == 200, response.content
    by_id = {row["id"]: row["sale_source"] for row in response.json()["results"]}
    assert by_id[str(comp.id)] == "box_office_comp"
    assert by_id[str(legacy.id)] is None  # created before the source was recorded
