"""Member-card door-scan service, companion to ticket check-in (``check_in_service``)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404

from accounts.models import RevelUser
from events.models import Event, OrganizationMember, Ticket
from events.service import day_membership_service
from events.service.check_in_service import check_in_ticket
from events.service.user_note_service import get_note_body


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
    private_note: str | None = None
    membership: day_membership_service.MembershipSummary | None = None


def scan_member_code(
    event: Event,
    code: str,
    checked_in_by: RevelUser,
    price_paid: Decimal | None = None,
    *,
    include_note: bool = False,
) -> MemberScanResult:
    """Resolve a ``member:<uuid>`` scan against an event.

    Report-only by design: a membership card is an identity credential, not an
    admission instrument. The single-ticket fast path delegates to
    ``check_in_ticket`` so status errors (already checked in, cancelled,
    window closed) surface exactly as they would for a direct ticket scan.

    ``include_note`` loads the organization's private note about the member (off by default;
    callers pass ``False`` for OAuth app tokens).
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
        checked_in = check_in_ticket(
            event, tickets[0].id, checked_in_by, price_paid=price_paid, include_note=include_note
        )
        return MemberScanResult(
            member=member,
            tickets=tickets,
            checked_in=checked_in,
            private_note=getattr(checked_in, "private_note", None) if include_note else None,
        )
    return MemberScanResult(
        member=member,
        tickets=tickets,
        checked_in=None,
        private_note=get_note_body(event.organization_id, member.user_id) if include_note else None,
        membership=day_membership_service.membership_summary(event.organization_id, member.user_id, ticket=None),
    )
