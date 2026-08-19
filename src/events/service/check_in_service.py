"""Door check-in: scanned-code resolution and the check-in transition itself.

Extracted verbatim from ``ticket_service`` (which had reached the 1000-line ceiling);
the behaviour and the public names are unchanged.
"""

from __future__ import annotations

import typing as t
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import formats, timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, Ticket, TicketTier
from events.service.seating.pricing import price_paid_is_admin_entered


def _format_in_event_tz(dt: datetime, event: Event) -> str:
    """Format ``dt`` in the event's local timezone (via its city), falling back to Django's active timezone.

    The tz abbreviation (``CET``, ``UTC``, …) is appended so the user can't misread an ambiguous local time.
    """
    tz: ZoneInfo | t.Any
    if event.city and event.city.timezone:
        try:
            tz = ZoneInfo(event.city.timezone)
        except KeyError:
            tz = timezone.get_current_timezone()
    else:
        tz = timezone.get_current_timezone()
    local = dt.astimezone(tz)
    return f"{formats.date_format(local, 'DATETIME_FORMAT', use_l10n=True)} {local.tzname() or ''}".rstrip()


def _check_in_closed_message(event: Event) -> str:
    """Build a localized error message for a closed check-in window, surfacing the open/close time when known."""
    if event.status != event.EventStatus.OPEN:
        return str(_("Check-in is not currently open for this event."))
    now = timezone.now()
    starts_at = event.check_in_starts_at or event.start
    ends_at = event.check_in_ends_at or event.end
    if now < starts_at:
        return str(_("Check-in is not open yet. It will open at {opens_at}.")).format(
            opens_at=_format_in_event_tz(starts_at, event)
        )
    if now > ends_at:
        return str(_("Check-in has closed for this event. It ended at {ended_at}.")).format(
            ended_at=_format_in_event_tz(ends_at, event)
        )
    return str(_("Check-in is not currently open for this event."))


def resolve_check_in_ticket_id(event: Event, code: str) -> UUID:
    """Resolve a scanned code (ticket UUID or ``series:<held-pass-uuid>``) to a ticket id.

    Malformed UUIDs 404 here so the ORM never raises on a bad lookup value.
    """
    if code.startswith(HeldSeriesPass.QR_PREFIX):
        try:
            held_pass_id = UUID(code[len(HeldSeriesPass.QR_PREFIX) :])
        except ValueError:
            raise Http404("Invalid pass code.") from None
        ticket = get_object_or_404(
            Ticket.objects.exclude(status=Ticket.TicketStatus.CANCELLED),
            held_pass_id=held_pass_id,
            event=event,
        )
        return ticket.id
    try:
        return UUID(code)
    except ValueError:
        raise Http404("Invalid ticket code.") from None


def _pending_check_in_allowed(ticket: Ticket) -> bool:
    """Whether a PENDING ticket may be checked in (payment collected at the door).

    For series-pass tickets the PASS's payment method is authoritative, not the
    mapped tier's — a pass paid online can be mapped to an offline tier and vice versa.
    """
    if ticket.held_pass is not None:
        return ticket.held_pass.series_pass.payment_method == TicketTier.PaymentMethod.OFFLINE
    return ticket.tier.payment_method == TicketTier.PaymentMethod.OFFLINE


def check_in_ticket(
    event: Event, ticket_id: UUID, checked_in_by: RevelUser, price_paid: Decimal | None = None
) -> Ticket:
    """Check in an attendee by scanning their ticket.

    Args:
        event: The event the ticket belongs to.
        ticket_id: UUID of the ticket to check in.
        checked_in_by: The user performing the check-in.
        price_paid: Amount paid. Required for PWYC offline/at-the-door tickets
            that don't have a price recorded yet. Optional as an override for
            PWYC offline/at-the-door tickets that already have a price.
            Forbidden for non-PWYC or online tickets.

    Note:
        price_paid is intentionally not validated against the tier's pwyc_min/pwyc_max
        bounds. Admins are trusted to override these limits at check-in.
    """
    # tier__* + the M2M prefetch cover CheckInResponseSchema's nested TicketTierSchema;
    # seat/sector feed the seat display. Trims ~4 queries per scan.
    ticket_qs = Ticket.objects.select_related(
        "user", "tier__event__organization", "tier__venue", "tier__sector", "held_pass__series_pass", "seat", "sector"
    ).prefetch_related("tier__restricted_to_membership_tiers")
    ticket = get_object_or_404(ticket_qs, pk=ticket_id, event=event)

    # Check if ticket status is valid for check-in
    # ACTIVE tickets can be checked in directly.
    # PENDING tickets are only allowed when payment is collected at the door
    # (see _pending_check_in_allowed for the series-pass vs tier distinction).
    # AT_THE_DOOR tickets are now created as ACTIVE, so no special handling needed.
    if ticket.status != Ticket.TicketStatus.ACTIVE:
        if not (ticket.status == Ticket.TicketStatus.PENDING and _pending_check_in_allowed(ticket)):
            # Determine appropriate error message based on ticket status
            if ticket.status == Ticket.TicketStatus.CHECKED_IN:
                error_message = str(_("This ticket has already been checked in."))
            elif ticket.status == Ticket.TicketStatus.CANCELLED:
                error_message = str(_("This ticket has been cancelled."))
            elif ticket.status == Ticket.TicketStatus.PENDING:
                error_message = str(_("This ticket is pending payment confirmation."))
            else:
                error_message = str(_("Invalid ticket status: {status}")).format(status=ticket.status)
            raise HttpError(400, error_message)

    # Check if check-in window is open
    if not event.is_check_in_open():
        raise HttpError(400, _check_in_closed_message(event))

    # May door staff type a price onto this ticket? Same authority as confirm/unconfirm
    # (spec §5.5), narrowed twice: pass tickets never carry a per-ticket price (the pass
    # itself was paid), and online tickets are settled by Payment.amount. Narrowing is
    # allowed; widening is not — a resolved price_paid must never be typed over here, and
    # any future undo-check-in must clear only what this predicate owns.
    is_pwyc_offsite = (
        ticket.held_pass_id is None
        and price_paid_is_admin_entered(ticket.tier)
        and ticket.tier.payment_method
        in (
            TicketTier.PaymentMethod.OFFLINE,
            TicketTier.PaymentMethod.AT_THE_DOOR,
        )
    )

    if not is_pwyc_offsite and price_paid is not None:
        raise HttpError(400, str(_("Price paid is not allowed for this ticket.")))

    if is_pwyc_offsite and price_paid is None and ticket.price_paid is None:
        raise HttpError(400, str(_("Price paid is required for Pay What You Can tickets without a recorded payment.")))

    # Update ticket status
    update_fields = ["status", "checked_in_at", "checked_in_by"]
    if is_pwyc_offsite and price_paid is not None:
        ticket.price_paid = price_paid
        update_fields.append("price_paid")

    ticket.status = Ticket.TicketStatus.CHECKED_IN
    ticket.checked_in_at = timezone.now()
    ticket.checked_in_by = checked_in_by
    ticket.save(update_fields=update_fields)

    return ticket
