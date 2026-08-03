"""ticket_guest_name_service.update_guest_name rules (#845)."""

import pytest
from ninja.errors import HttpError

from events.models import Ticket
from events.service import ticket_guest_name_service


@pytest.mark.django_db
class TestUpdateGuestName:
    def test_renames_active_ticket(self, ticket: Ticket) -> None:
        updated = ticket_guest_name_service.update_guest_name(ticket, "New Name")
        assert updated.guest_name == "New Name"
        ticket.refresh_from_db()
        assert ticket.guest_name == "New Name"

    def test_strips_surrounding_whitespace(self, ticket: Ticket) -> None:
        assert ticket_guest_name_service.update_guest_name(ticket, "  New Name  ").guest_name == "New Name"

    def test_renames_pending_ticket(self, ticket: Ticket) -> None:
        ticket.status = Ticket.TicketStatus.PENDING
        ticket.save(update_fields=["status"])
        assert ticket_guest_name_service.update_guest_name(ticket, "New Name").guest_name == "New Name"

    @pytest.mark.parametrize("status", [Ticket.TicketStatus.CHECKED_IN, Ticket.TicketStatus.CANCELLED])
    def test_refuses_terminal_states_with_409(self, ticket: Ticket, status: str) -> None:
        ticket.status = status
        ticket.save(update_fields=["status"])
        with pytest.raises(HttpError) as exc:
            ticket_guest_name_service.update_guest_name(ticket, "New Name")
        assert exc.value.status_code == 409

    def test_clearing_allowed_only_when_event_does_not_require_names(self, ticket: Ticket) -> None:
        ticket.event.require_ticket_names = False
        ticket.event.save(update_fields=["require_ticket_names"])
        assert ticket_guest_name_service.update_guest_name(ticket, "").guest_name == ""

    def test_clearing_refused_with_400_when_names_required(self, ticket: Ticket) -> None:
        ticket.event.require_ticket_names = True
        ticket.event.save(update_fields=["require_ticket_names"])
        with pytest.raises(HttpError) as exc:
            ticket_guest_name_service.update_guest_name(ticket, "   ")
        assert exc.value.status_code == 400
