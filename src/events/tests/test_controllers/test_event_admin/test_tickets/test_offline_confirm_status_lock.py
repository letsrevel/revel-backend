"""Offline payment confirm and unconfirm re-check the ticket status under a lock (#1460)."""

import typing as t
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja.errors import HttpError

from events.models import Event, Ticket, TicketTier
from events.service import ticket_service
from events.service.ticket_service import lock_tier_then_ticket

pytestmark = pytest.mark.django_db


def _stale(ticket: Ticket, now_status: str) -> Ticket:
    """Return the caller's unlocked instance after another request changed the row's status."""
    stale = Ticket.objects.select_related("tier").get(pk=ticket.pk)
    Ticket.objects.filter(pk=ticket.pk).update(status=now_status)
    return stale


@pytest.mark.parametrize("now_status", [Ticket.TicketStatus.CANCELLED, Ticket.TicketStatus.ACTIVE])
def test_confirm_refuses_a_ticket_that_is_no_longer_pending(pending_offline_ticket: Ticket, now_status: str) -> None:
    stale = _stale(pending_offline_ticket, now_status)

    with pytest.raises(HttpError) as exc:
        ticket_service.confirm_ticket_payment(stale)

    assert exc.value.status_code == 409
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == now_status


@pytest.mark.parametrize("now_status", [Ticket.TicketStatus.CHECKED_IN, Ticket.TicketStatus.CANCELLED])
def test_unconfirm_refuses_a_ticket_that_is_no_longer_active(pending_offline_ticket: Ticket, now_status: str) -> None:
    Ticket.objects.filter(pk=pending_offline_ticket.pk).update(status=Ticket.TicketStatus.ACTIVE)
    stale = _stale(pending_offline_ticket, now_status)

    with pytest.raises(HttpError) as exc:
        ticket_service.unconfirm_ticket_payment(stale)

    assert exc.value.status_code == 409
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == now_status


def test_confirm_and_unconfirm_still_work_on_a_current_ticket(pending_offline_ticket: Ticket) -> None:
    ticket = Ticket.objects.select_related("tier").get(pk=pending_offline_ticket.pk)

    assert ticket_service.confirm_ticket_payment(ticket).status == Ticket.TicketStatus.ACTIVE
    ticket = Ticket.objects.select_related("tier").get(pk=pending_offline_ticket.pk)
    assert ticket_service.unconfirm_ticket_payment(ticket).status == Ticket.TicketStatus.PENDING


def test_the_endpoint_answers_409_when_the_ticket_changes_during_the_request(
    organization_owner_client: Client, event: Event, pending_offline_ticket: Ticket
) -> None:
    def cancel_then_lock(tickets: t.Any) -> list[Ticket]:
        Ticket.objects.filter(pk=pending_offline_ticket.pk).update(status=Ticket.TicketStatus.CANCELLED)
        return lock_tier_then_ticket(tickets)

    url = reverse("api:confirm_ticket_payment", kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk})
    with patch("events.service.ticket_service.lock_tier_then_ticket", side_effect=cancel_then_lock):
        response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 409
    assert response.json()["detail"] == "This ticket has changed. Reload the page and try again."


def test_unconfirm_refuses_a_ticket_whose_tier_is_no_longer_offline(pending_offline_ticket: Ticket) -> None:
    Ticket.objects.filter(pk=pending_offline_ticket.pk).update(status=Ticket.TicketStatus.ACTIVE)
    stale = Ticket.objects.select_related("tier").get(pk=pending_offline_ticket.pk)
    TicketTier.objects.filter(pk=stale.tier_id).update(payment_method=TicketTier.PaymentMethod.AT_THE_DOOR)

    with pytest.raises(HttpError) as exc:
        ticket_service.unconfirm_ticket_payment(stale)

    assert exc.value.status_code == 409


def test_confirm_refuses_a_ticket_whose_tier_is_now_paid_online(pending_offline_ticket: Ticket) -> None:
    stale = Ticket.objects.select_related("tier").get(pk=pending_offline_ticket.pk)
    TicketTier.objects.filter(pk=stale.tier_id).update(payment_method=TicketTier.PaymentMethod.ONLINE)

    with pytest.raises(HttpError) as exc:
        ticket_service.confirm_ticket_payment(stale)

    assert exc.value.status_code == 409
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.PENDING
