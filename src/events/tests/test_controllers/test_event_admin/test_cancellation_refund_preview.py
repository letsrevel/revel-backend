"""Tests for cancel-with-refund flag validation and the balance preview route (#865, task 8)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def test_refund_tickets_rejected_off_target_status(organization_owner_client: Client, event: Event) -> None:
    """``refund_tickets=True`` is only valid when the target status is 'cancelled'."""
    event.status = Event.EventStatus.DRAFT
    event.save(update_fields=["status"])

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url, data={"refund_tickets": True}, content_type="application/json")

    assert response.status_code == 400, response.content
    event.refresh_from_db()
    assert event.status == Event.EventStatus.DRAFT


class TestCancellationRefundPreview:
    def test_preview_happy_path(
        self,
        organization_owner_client: Client,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        offline = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("20.00"))
        online_ticket = ticket_factory(tier=online)
        payment_factory(
            ticket=online_ticket,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_preview",
        )
        ticket_factory(tier=offline)

        url = reverse("api:event_cancellation_refund_preview", kwargs={"event_id": event.pk})
        with patch("stripe.Balance.retrieve") as mock_balance:
            mock_balance.return_value.available = [{"currency": "eur", "amount": 10000}]
            response = organization_owner_client.get(url)

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["active_tickets"] == 2
        assert data["online_refundable_tickets"] == 1
        assert data["offline_tickets"] == 1
        assert len(data["currencies"]) == 1
        line = data["currencies"][0]
        assert line["currency"] == "EUR"
        assert Decimal(line["total_refundable"]) == Decimal("40.00")
        assert Decimal(line["available_balance"]) == Decimal("100.00")
        assert line["balance_sufficient"] is True
        assert data["tickets_refund_started_at"] is None

    def test_requires_manage_event_permission(
        self, organization_staff_client: Client, event: Event, staff_member: t.Any
    ) -> None:
        perms = staff_member.permissions
        perms["default"]["manage_event"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:event_cancellation_refund_preview", kwargs={"event_id": event.pk})
        response = organization_staff_client.get(url)

        assert response.status_code == 403, response.content
