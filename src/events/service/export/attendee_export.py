"""Generate Excel export of event attendee list."""

import typing as t
from io import BytesIO
from uuid import UUID

import structlog
from openpyxl import Workbook

from common.models import FileExport
from common.service.export_service import complete_export, fail_export, notify_export_ready, start_export
from events.models import Event, EventRSVP, Ticket

logger = structlog.get_logger(__name__)


def generate_attendee_export(export_id: UUID) -> None:
    """Build an Excel workbook with attendee data for an event."""
    export = FileExport.objects.select_related("requested_by").get(pk=export_id)
    start_export(export)
    try:
        file_bytes = _build_attendee_workbook(export)
        complete_export(export, file_bytes, f"attendee_export_{export_id}.xlsx")
        notify_export_ready(export)
        logger.info("attendee_export_completed", export_id=str(export_id))
    except Exception as e:
        fail_export(export, f"Export failed: {e}")
        logger.exception("attendee_export_failed", export_id=str(export_id))
        raise


def _build_attendee_workbook(export: FileExport) -> bytes:
    """Build the Excel workbook and return raw bytes."""
    event_id = UUID(export.parameters["event_id"])
    event = Event.objects.select_related("venue", "organization").get(pk=event_id)

    tickets = list(
        Ticket.objects.filter(
            event=event,
            status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN],
        ).select_related("user", "tier", "seat", "seat__sector", "payment")
    )

    rsvps = list(EventRSVP.objects.filter(event=event, status=EventRSVP.RsvpStatus.YES).select_related("user"))

    ticket_checked_in = sum(1 for tk in tickets if tk.status == Ticket.TicketStatus.CHECKED_IN)

    wb = Workbook()
    _write_summary_sheet(wb, event, tickets, rsvps, ticket_checked_in)
    _write_attendees_sheet(wb, tickets, rsvps)

    buf = BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()
    buf.close()
    wb.close()
    return file_bytes


def _write_summary_sheet(
    wb: Workbook,
    event: Event,
    tickets: list[Ticket],
    rsvps: list[EventRSVP],
    ticket_checked_in: int,
) -> None:
    """Populate the Summary sheet."""
    ws = wb.active
    if ws is None:
        ws = wb.create_sheet()
    ws.title = "Summary"

    summary_rows: list[tuple[str, t.Any]] = [
        ("Event", event.name),
        ("Date", event.start.isoformat() if event.start else "N/A"),
        ("Venue", event.venue.name if event.venue else "N/A"),
        ("Organization", event.organization.name),
        ("Total attendees", len(tickets) + len(rsvps)),
        ("Tickets", len(tickets)),
        ("RSVPs", len(rsvps)),
        ("Checked in", ticket_checked_in),
    ]
    for row in summary_rows:
        ws.append(row)


def _write_attendees_sheet(wb: Workbook, tickets: list[Ticket], rsvps: list[EventRSVP]) -> None:
    """Populate the Attendees sheet."""
    ws = wb.create_sheet("Attendees")
    headers = [
        "Name",
        "Email",
        "Attendance Type",
        "Ticket Tier",
        "Ticket Status",
        "Check-in Status",
        "Checked In At",
        "Guest Name",
        "Seat",
        "Payment Status",
    ]
    ws.append(headers)

    for ticket in tickets:
        payment = getattr(ticket, "payment", None)
        seat_label = ticket.seat.label if ticket.seat else ""
        ws.append(
            [
                ticket.user.get_full_name() if ticket.user else "",
                ticket.user.email if ticket.user else "",
                "ticket",
                ticket.tier.name if ticket.tier else "",
                ticket.status,
                "checked_in" if ticket.status == Ticket.TicketStatus.CHECKED_IN else "not_checked_in",
                ticket.checked_in_at.isoformat() if ticket.checked_in_at else "",
                ticket.guest_name or "",
                seat_label,
                payment.status if payment else (ticket.tier.payment_method if ticket.tier else ""),
            ]
        )

    for rsvp in rsvps:
        ws.append(
            [
                rsvp.user.get_full_name() if rsvp.user else "",
                rsvp.user.email if rsvp.user else "",
                "rsvp",
                "",  # tier
                "",  # ticket status
                "",  # check-in status
                "",  # checked in at
                "",  # guest name
                "",  # seat
                "",  # payment status
            ]
        )
