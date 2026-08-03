"""ticket_guest_name_service.update_guest_name rules (#845)."""

import pytest
from ninja.errors import HttpError

from events.models import Ticket
from events.service import ticket_file_service, ticket_guest_name_service


@pytest.mark.django_db
class TestUpdateGuestName:
    def test_renames_active_ticket(self, ticket: Ticket) -> None:
        updated = ticket_guest_name_service.update_guest_name(ticket, "New Name")
        assert updated.guest_name == "New Name"
        ticket.refresh_from_db()
        assert ticket.guest_name == "New Name"

    def test_strips_surrounding_whitespace(self, ticket: Ticket) -> None:
        assert ticket_guest_name_service.update_guest_name(ticket, "  New Name  ").guest_name == "New Name"

    def test_rename_invalidates_cached_ticket_files(self, ticket: Ticket) -> None:
        """A rename must bust the PDF/pkpass cache — guest_name renders into both artifacts.

        The cache key is built from ``updated_at`` timestamps, so the save must include
        ``updated_at`` in ``update_fields`` or ``auto_now`` never fires and stale files
        are served forever (#845).
        """
        ticket.file_content_hash = ticket_file_service.compute_content_hash(ticket)
        ticket.save(update_fields=["file_content_hash"])
        assert ticket_file_service.is_cache_valid(ticket) is True

        ticket_guest_name_service.update_guest_name(ticket, "New Name")

        assert ticket_file_service.is_cache_valid(ticket) is False
        ticket.refresh_from_db()
        assert ticket_file_service.is_cache_valid(ticket) is False

    def test_renaming_to_the_same_name_keeps_cached_files_valid(self, ticket: Ticket) -> None:
        """A no-op rename must not bump ``updated_at`` — the PDF/pkpass are still correct."""
        ticket.file_content_hash = ticket_file_service.compute_content_hash(ticket)
        ticket.save(update_fields=["file_content_hash"])
        assert ticket_file_service.is_cache_valid(ticket) is True

        ticket_guest_name_service.update_guest_name(ticket, f"  {ticket.guest_name}  ")

        assert ticket_file_service.is_cache_valid(ticket) is True
        ticket.refresh_from_db()
        assert ticket_file_service.is_cache_valid(ticket) is True

    def test_no_op_rename_still_refused_on_a_checked_in_ticket(self, ticket: Ticket) -> None:
        """The status gate runs first: a frozen ticket is a 409 even when nothing would change."""
        ticket.status = Ticket.TicketStatus.CHECKED_IN
        ticket.save(update_fields=["status"])
        with pytest.raises(HttpError) as exc:
            ticket_guest_name_service.update_guest_name(ticket, ticket.guest_name)
        assert exc.value.status_code == 409

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
