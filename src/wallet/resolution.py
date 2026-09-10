"""Shared field resolution for wallet passes (Apple and Google rails).

Both rails answer the same two questions before they can render a pass — *where
is this ticket* and *what window does this series pass cover* — and both used to
answer them with textually duplicated blocks, so a fix to one rail silently
missed the other. They live here instead, as pure functions over already-loaded
relations (same shape as :mod:`wallet.pricing`).

Imagery deliberately stays with each rail: Apple embeds image *bytes* in the
archive, Google emits *URLs* its servers fetch. Only the representative event is
resolved here; each rail derives its own artwork from it.
"""

import typing as t
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from events.models import Event, HeldSeriesPass, Ticket, Venue, VenueSector
from events.utils import get_event_timezone, get_organization_timezone


class TicketLocation(t.NamedTuple):
    """Where a ticket sits: venue, sector and seat, with the display strings."""

    venue: Venue | None
    venue_name: str | None
    address: str | None
    sector: VenueSector | None
    seat_label: str | None


def resolve_ticket_location(ticket: Ticket) -> TicketLocation:
    """Resolve a ticket's venue, sector and seat.

    Priority (most specific first): the tier's venue, then the ticket's own
    venue, then the event's; the tier's sector, then the ticket's. The address
    prefers the resolved venue's full address and falls back to the event's
    free-text address.

    Every input is already loaded on the pass-generating paths
    (``Ticket.objects.full()`` selects ``tier``, ``seat`` and ``event``), so this
    is a pure function over them — no extra query per pass.

    Args:
        ticket: The ticket to locate.

    Returns:
        TicketLocation: The resolved venue/sector/seat, with ``None`` for
        whatever the ticket does not carry.
    """
    event = ticket.event
    venue = ticket.tier.venue or ticket.venue or event.venue
    sector = ticket.tier.sector or ticket.sector

    return TicketLocation(
        venue=venue,
        venue_name=venue.name if venue else None,
        address=(venue.full_address() if venue else None) or event.address or None,
        sector=sector,
        seat_label=ticket.seat.label if ticket.seat else None,
    )


class SeriesWindow(t.NamedTuple):
    """The event-shaped fields a series pass borrows from its covered events."""

    representative_event: Event | None
    venue: Venue | None
    venue_name: str | None
    address: str | None
    tz: ZoneInfo
    start: datetime
    end: datetime


def resolve_series_window(held_pass: HeldSeriesPass) -> SeriesWindow:
    """Resolve the representative event and validity window of a held series pass.

    A series pass has no single event, so the pass borrows: the representative
    is the soonest covered event that has not yet ended, falling back to the
    latest-starting past one once the series is over. ``start`` follows the
    representative; ``end`` is the latest end across *all* covered events, so the
    pass stays valid until the series is fully over.

    Args:
        held_pass: The held series pass to resolve a window for.

    Returns:
        SeriesWindow: The representative event (``None`` only for the defensive
        case of a pass covering no events, where the window collapses to the
        pass's creation time) with its venue, timezone and window.
    """
    events = [link.event for link in held_pass.series_pass.tier_links.select_related("event").all()]
    now = timezone.now()
    upcoming = sorted((event for event in events if event.end >= now), key=lambda event: event.start)
    representative = upcoming[0] if upcoming else max(events, key=lambda event: event.start, default=None)

    if representative is None:
        # Defensive fallback: a purchasable series pass always covers at least
        # two events, so this only guards against an edge case with none.
        start = held_pass.created_at
        return SeriesWindow(
            representative_event=None,
            venue=None,
            venue_name=None,
            address=None,
            tz=get_organization_timezone(held_pass.series_pass.event_series.organization),
            start=start,
            end=start,
        )

    venue = representative.venue
    return SeriesWindow(
        representative_event=representative,
        venue=venue,
        venue_name=venue.name if venue else None,
        address=(venue.full_address() if venue else None) or representative.address or None,
        tz=get_event_timezone(representative),
        start=representative.start,
        end=max(event.end for event in events),
    )
