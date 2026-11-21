"""Offline payment confirm and unconfirm re-check the ticket status under a lock (#1460)."""

import pytest
from ninja.errors import HttpError

from events.models import Ticket
from events.service import ticket_service

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
