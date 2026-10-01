"""Cached ticket PDFs/pkpasses go stale when the printed compliance lines change (EU layer 1).

Numbering (bulk_update), org billing edits and ``price_paid`` stamps don't bump any of
the timestamps the content hash used to cover, so the hash includes status and the
rendered compliance values.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from events.models import Organization, Ticket, TicketTier
from events.service import ticket_file_service
from events.service.ticket_number_service import assign_ticket_numbers, format_ticket_number

pytestmark = pytest.mark.django_db


def _hash(ticket: Ticket) -> str:
    fresh = Ticket.objects.full().get(pk=ticket.pk)
    return ticket_file_service.compute_content_hash(fresh)


def test_hash_changes_after_numbering_via_bulk_update(ticket: Ticket) -> None:
    Ticket.objects.filter(pk=ticket.pk).update(ticket_series="", ticket_number=None, issued_at=None)
    unnumbered = Ticket.objects.get(pk=ticket.pk)
    before = _hash(unnumbered)

    assign_ticket_numbers([unnumbered])  # bulk_update: updated_at untouched

    assert _hash(unnumbered) != before


@pytest.mark.parametrize(("field", "value"), [("billing_name", "New Legal Name Ltd"), ("vat_id", "ATU99999999")])
def test_hash_changes_after_org_billing_change(
    organization: Organization, ticket: Ticket, field: str, value: str
) -> None:
    before = _hash(ticket)

    Organization.objects.filter(pk=organization.pk).update(**{field: value})

    assert _hash(ticket) != before


def test_hash_changes_after_price_paid_change(ticket: Ticket) -> None:
    before = _hash(ticket)

    Ticket.objects.filter(pk=ticket.pk).update(price_paid=Decimal("3.50"))

    assert _hash(ticket) != before


def test_pdf_cached_while_pending_is_regenerated_with_the_number(ticket: Ticket, event_ticket_tier: TicketTier) -> None:
    pending = Ticket.objects.create(
        event=ticket.event, tier=event_ticket_tier, user=ticket.user, status=Ticket.TicketStatus.PENDING
    )

    def render(t_: Ticket) -> bytes:
        return f"PDF {format_ticket_number(t_) or 'unnumbered'}".encode()

    with patch("events.utils.create_ticket_pdf", side_effect=render):
        cached = ticket_file_service.get_or_generate_pdf(Ticket.objects.full().get(pk=pending.pk))
        assert cached == b"PDF unnumbered"

        pending.status = Ticket.TicketStatus.ACTIVE
        pending.save(update_fields=["status"])  # numbering happens in post_save via bulk_update

        confirmed = Ticket.objects.full().get(pk=pending.pk)
        assert not ticket_file_service.is_cache_valid(confirmed)
        regenerated = ticket_file_service.get_or_generate_pdf(confirmed)

    assert format_ticket_number(confirmed) in regenerated.decode()
    assert regenerated != cached
