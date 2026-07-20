"""Sparse per-event seat availability (spec §2).

Only non-available seats are reported; anything absent from ``seats`` is available.
Precedence: sold > blocked (override or decommissioned) > held.
"""

import datetime
import typing as t
import uuid

from django.db.models import Count, Max, OuterRef, Q, Subquery

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, SeatHold, Ticket, Venue, VenueSeat, VenueSector
from events.schema.seating import SeatingAvailabilitySchema, StandingAvailabilitySchema


def resolve_chart_version(venue_id: uuid.UUID) -> datetime.datetime | None:
    """Compute the venue chart's ``updated_at`` without building the chart.

    Must stay identical to what :func:`events.service.seating.chart.build_chart` puts in
    ``VenueChartSchema.updated_at`` — the max of the venue's, its sectors' and its seats'
    timestamps — otherwise the poller either never refetches or refetches forever. Doing it
    with two correlated subqueries keeps it to a single round trip; materializing the chart
    (411 KB) just to read a timestamp would not be acceptable on a polled endpoint.

    Args:
        venue_id: The venue whose chart version is wanted.

    Returns:
        The chart's version timestamp, or ``None`` if the venue no longer exists.
    """
    row = (
        Venue.objects.filter(pk=venue_id)
        .annotate(
            sectors_updated_at=Subquery(
                VenueSector.objects.filter(venue_id=OuterRef("pk"))
                .values("venue_id")
                .annotate(latest=Max("updated_at"))
                .values("latest")
            ),
            seats_updated_at=Subquery(
                VenueSeat.objects.filter(sector__venue_id=OuterRef("pk"))
                .values("sector__venue_id")
                .annotate(latest=Max("updated_at"))
                .values("latest")
            ),
        )
        .values("updated_at", "sectors_updated_at", "seats_updated_at")
        .first()
    )
    if row is None:
        return None
    return max(t.cast("datetime.datetime", value) for value in row.values() if value is not None)


def build_availability(event: Event, *, user: RevelUser | None, guest_session: str | None) -> SeatingAvailabilitySchema:
    """Build the sparse availability payload for one event's seated + standing sectors."""
    seats: dict[uuid.UUID, str] = {}

    # Non-cancelled = occupied, matching the unique_ticket_event_seat constraint
    # (a CHECKED_IN seat is just as taken as an ACTIVE one).
    sold = (
        Ticket.objects.filter(event=event, seat__isnull=False)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("seat_id", flat=True)
    )
    for sid in sold:
        seats[sid] = "sold"

    for sid in EventSeatOverride.objects.filter(event=event).values_list("seat_id", flat=True):
        seats.setdefault(sid, "blocked")

    if event.venue_id:
        inactive = VenueSeat.objects.filter(sector__venue_id=event.venue_id, is_active=False)
        for sid in inactive.values_list("id", flat=True):
            seats.setdefault(sid, "blocked")

    for sid in SeatHold.objects.active().filter(event=event).values_list("seat_id", flat=True):
        seats.setdefault(sid, "held")

    own = list(SeatHold.objects.active().filter(SeatHold.owner_q(user, guest_session), event=event))
    my_holds = [h.seat_id for h in own]
    my_expiry = min((h.expires_at for h in own), default=None)

    standing: dict[uuid.UUID, StandingAvailabilitySchema] = {}
    if event.venue_id:
        rows = (
            VenueSector.objects.filter(venue_id=event.venue_id, kind=VenueSector.Kind.STANDING)
            .annotate(
                taken=Count(
                    "tickets",
                    filter=Q(tickets__event=event) & ~Q(tickets__status=Ticket.TicketStatus.CANCELLED),
                )
            )
            .values("id", "capacity", "taken")
        )
        standing = {r["id"]: StandingAvailabilitySchema(capacity=r["capacity"], taken=r["taken"]) for r in rows}

    return SeatingAvailabilitySchema(
        seats=seats,
        standing=standing,
        my_holds=my_holds,
        my_holds_expire_at=my_expiry,
        chart_updated_at=resolve_chart_version(event.venue_id) if event.venue_id else None,
    )
