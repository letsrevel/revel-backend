"""Tests for confirm-payment endpoint with PWYC tiers."""

import json

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Event, Ticket

pytestmark = pytest.mark.django_db


def test_confirm_payment_pwyc_requires_price_paid(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket: Ticket,
) -> None:
    """Test that PWYC ticket confirmation without price_paid returns 400."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket.pk},
    )
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400

    # Verify ticket status unchanged
    pending_pwyc_offline_ticket.refresh_from_db()
    assert pending_pwyc_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_confirm_payment_pwyc_with_valid_price(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket: Ticket,
) -> None:
    """Test that PWYC ticket with valid price_paid is confirmed successfully."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket.pk},
    )
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "15.00"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Ticket.TicketStatus.ACTIVE
    assert data["price_paid"] == "15.00"

    # Verify in database
    pending_pwyc_offline_ticket.refresh_from_db()
    assert pending_pwyc_offline_ticket.status == Ticket.TicketStatus.ACTIVE
    assert str(pending_pwyc_offline_ticket.price_paid) == "15.00"


def test_confirm_payment_pwyc_zero_price(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket: Ticket,
) -> None:
    """Test that PWYC ticket with price_paid=0 returns 422 (Pydantic gt=0 validation)."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket.pk},
    )
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "0"}),
        content_type="application/json",
    )

    assert response.status_code == 422

    # Verify ticket status unchanged
    pending_pwyc_offline_ticket.refresh_from_db()
    assert pending_pwyc_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_confirm_payment_fixed_tier_rejects_price_paid(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Test that fixed-price tier rejects price_paid in payload with 400."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
    )
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "25.00"}),
        content_type="application/json",
    )

    assert response.status_code == 400

    # Verify ticket status unchanged
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_confirm_payment_pwyc_negative_price(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket: Ticket,
) -> None:
    """Test that PWYC ticket with negative price_paid returns 422 (Pydantic gt=0 validation)."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket.pk},
    )
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "-5.00"}),
        content_type="application/json",
    )

    assert response.status_code == 422

    # Verify ticket status unchanged
    pending_pwyc_offline_ticket.refresh_from_db()
    assert pending_pwyc_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_confirm_payment_pwyc_at_door_with_valid_price(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_at_door_ticket: Ticket,
) -> None:
    """Test that PWYC at-the-door ticket with valid price_paid is confirmed."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_at_door_ticket.pk},
    )
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "20.00"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Ticket.TicketStatus.ACTIVE
    assert data["price_paid"] == "20.00"

    pending_pwyc_at_door_ticket.refresh_from_db()
    assert pending_pwyc_at_door_ticket.status == Ticket.TicketStatus.ACTIVE
    assert str(pending_pwyc_at_door_ticket.price_paid) == "20.00"


def test_confirm_payment_pwyc_at_door_requires_price_paid(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_at_door_ticket: Ticket,
) -> None:
    """Test that PWYC at-the-door ticket without price_paid returns 400."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_at_door_ticket.pk},
    )
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400

    pending_pwyc_at_door_ticket.refresh_from_db()
    assert pending_pwyc_at_door_ticket.status == Ticket.TicketStatus.PENDING


def test_confirm_payment_pwyc_with_existing_price_no_override(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket_with_price: Ticket,
) -> None:
    """PWYC ticket with existing price_paid confirms without requiring price in payload."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket_with_price.pk},
    )
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Ticket.TicketStatus.ACTIVE
    assert data["price_paid"] == "10.00"

    pending_pwyc_offline_ticket_with_price.refresh_from_db()
    assert pending_pwyc_offline_ticket_with_price.status == Ticket.TicketStatus.ACTIVE
    assert str(pending_pwyc_offline_ticket_with_price.price_paid) == "10.00"


def test_confirm_payment_pwyc_with_existing_price_override(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket_with_price: Ticket,
) -> None:
    """PWYC ticket with existing price_paid can be overridden by admin."""
    url = reverse(
        "api:confirm_ticket_payment",
        kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket_with_price.pk},
    )
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "20.00"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Ticket.TicketStatus.ACTIVE
    assert data["price_paid"] == "20.00"

    pending_pwyc_offline_ticket_with_price.refresh_from_db()
    assert pending_pwyc_offline_ticket_with_price.status == Ticket.TicketStatus.ACTIVE
    assert str(pending_pwyc_offline_ticket_with_price.price_paid) == "20.00"
