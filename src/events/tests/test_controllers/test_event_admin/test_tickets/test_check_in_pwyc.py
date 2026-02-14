"""Tests for check-in endpoint with PWYC price_paid handling."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event) -> None:
    """Ensure the check-in window is open for all tests in this module."""
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


def test_check_in_pwyc_offline_pending_with_price(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket: Ticket,
) -> None:
    """Pending PWYC offline ticket with price_paid provided — checks in and saves price."""
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket.pk})
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "12.50"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Ticket.TicketStatus.CHECKED_IN
    assert data["price_paid"] == "12.50"

    pending_pwyc_offline_ticket.refresh_from_db()
    assert pending_pwyc_offline_ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert pending_pwyc_offline_ticket.price_paid == Decimal("12.50")


def test_check_in_pwyc_offline_pending_without_price(
    organization_owner_client: Client,
    event: Event,
    pending_pwyc_offline_ticket: Ticket,
) -> None:
    """Pending PWYC offline ticket without price_paid — 400 error."""
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": pending_pwyc_offline_ticket.pk})
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400

    pending_pwyc_offline_ticket.refresh_from_db()
    assert pending_pwyc_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_check_in_pwyc_at_door_active_with_price(
    organization_owner_client: Client,
    event: Event,
    pwyc_at_door_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    """Active PWYC at_the_door ticket (no price set) with price_paid — checks in and saves price."""
    ticket = Ticket.objects.create(
        guest_name="PWYC Door",
        user=member_user,
        event=event,
        tier=pwyc_at_door_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "25.00"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert ticket.price_paid == Decimal("25.00")


def test_check_in_pwyc_at_door_active_without_price(
    organization_owner_client: Client,
    event: Event,
    pwyc_at_door_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    """Active PWYC at_the_door ticket (no price set) without price_paid — 400 error."""
    ticket = Ticket.objects.create(
        guest_name="PWYC Door",
        user=member_user,
        event=event,
        tier=pwyc_at_door_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400
    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_check_in_pwyc_price_already_set(
    organization_owner_client: Client,
    event: Event,
    pwyc_offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """PWYC ticket with price already recorded — checks in normally without price_paid."""
    ticket = Ticket.objects.create(
        guest_name="PWYC Confirmed",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
        price_paid=Decimal("10.00"),
    )

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert ticket.price_paid == Decimal("10.00")  # unchanged


def test_check_in_pwyc_price_already_set_accepts_override(
    organization_owner_client: Client,
    event: Event,
    pwyc_offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """PWYC ticket with price already recorded — accepts price_paid override at check-in."""
    ticket = Ticket.objects.create(
        guest_name="PWYC Confirmed",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
        price_paid=Decimal("10.00"),
    )

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": ticket.pk})
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "20.00"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert ticket.price_paid == Decimal("20.00")


def test_check_in_non_pwyc_rejects_price_paid(
    organization_owner_client: Client,
    event: Event,
    pending_offline_ticket: Ticket,
) -> None:
    """Non-PWYC pending offline ticket — rejects price_paid in payload."""
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk})
    response = organization_owner_client.post(
        url,
        data=json.dumps({"price_paid": "25.00"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.PENDING


def test_check_in_non_pwyc_no_price_works(
    organization_owner_client: Client,
    event: Event,
    active_online_ticket: Ticket,
) -> None:
    """Non-PWYC active ticket — checks in normally without price_paid."""
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CHECKED_IN
