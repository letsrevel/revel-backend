"""Member-card door-scan service, companion to ticket check-in (``ticket_service``)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404

from accounts.models import RevelUser
from events.models import Event, OrganizationMember, Ticket
from events.service.ticket_service import check_in_ticket


@dataclass
class MemberScanResult:
    """Outcome of scanning a ``member:`` code at an event door.

    ``checked_in`` is set only when the member had exactly one non-cancelled
    ticket and it passed ``check_in_ticket``'s gates. With several tickets the
    endpoint never guesses which one to burn — staff scan the ticket QR instead.
    """

    member: OrganizationMember
    tickets: list[Ticket]
    checked_in: Ticket | None


def scan_member_code(
    event: Event, code: str, checked_in_by: RevelUser, price_paid: Decimal | None = None
) -> MemberScanResult:
    """Resolve a ``member:<uuid>`` scan against an event.

    Report-only by design: a membership card is an identity credential, not an
    admission instrument. The single-ticket fast path delegates to
    ``check_in_ticket`` so status errors (already checked in, cancelled,
    window closed) surface exactly as they would for a direct ticket scan.
    """
    try:
        member_id = UUID(code[len(OrganizationMember.QR_PREFIX) :])
    except ValueError:
        raise Http404("Invalid member code.") from None
    member = get_object_or_404(
        OrganizationMember.objects.select_related("user", "tier"),
        pk=member_id,
        organization_id=event.organization_id,
    )
    tickets = list(
        Ticket.objects.select_related("tier", "held_pass__series_pass")
        .filter(event=event, user_id=member.user_id)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
    )
    if len(tickets) == 1:
        checked_in = check_in_ticket(event, tickets[0].id, checked_in_by, price_paid=price_paid)
        return MemberScanResult(member=member, tickets=tickets, checked_in=checked_in)
    return MemberScanResult(member=member, tickets=tickets, checked_in=None)
