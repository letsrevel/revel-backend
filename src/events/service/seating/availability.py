"""Sparse per-event seat availability (spec §2).

Only non-available seats are reported; anything absent from ``seats`` is available.
Precedence: sold > blocked (override or decommissioned) > held.
"""

import dataclasses
import datetime
import uuid

from django.db.models import Count, Q

from accounts.models import RevelUser
from events.models import Event, SeatHold, Ticket, Venue, VenueSeat, VenueSector
from events.schema.seating import SeatingAvailabilitySchema, StandingAvailabilitySchema, ZoneAvailabilitySchema
from events.service.seating.pick import load_taken_seats


def resolve_chart_version(venue_id: uuid.UUID) -> datetime.datetime | None:
    """Read the venue chart's ``updated_at`` without building the chart.

    Must stay identical to what :func:`events.service.seating.chart.build_chart` puts in
    ``VenueChartSchema.updated_at``, otherwise the poller either never refetches or refetches
    forever. They cannot drift because both now read the *same column*: ``Venue.chart_version``,
    written only by :func:`events.service.seating.chart.bump_chart_version`. This used to be a
    ``max()`` over the venue's, its sectors' and its seats' ``updated_at``, reproduced here with
    two correlated subqueries — two implementations of one value, blind to deletes and to every
    writer that bypassed ``auto_now`` (#752).

    Args:
        venue_id: The venue whose chart version is wanted.

    Returns:
        The chart's version timestamp, or ``None`` if the venue no longer exists.
    """
    return Venue.objects.filter(pk=venue_id).values_list("chart_version", flat=True).first()


@dataclasses.dataclass
class _ZoneAccumulator:
    """Per-(sector, category) tally built in one pass over the venue's painted seats."""

    order: tuple[int, int]
    free: int = 0
    accessible_free: int = 0
    # row_order -> adjacency indexes of the free non-accessible seats in that row
    rows: dict[int, list[int]] = dataclasses.field(default_factory=dict)


def _longest_run(adjacency_indexes: list[int]) -> int:
    """Longest run of consecutive adjacency indexes — mirrors ``_contiguous_runs`` scoring."""
    best = run = 0
    previous: int | None = None
    for index in sorted(adjacency_indexes):
        run = run + 1 if previous is not None and index == previous + 1 else 1
        previous = index
        best = max(best, run)
    return best


def build_zone_availability(event: Event, taken: set[uuid.UUID]) -> list[ZoneAvailabilitySchema]:
    """Summarize selectable seats per (sector, price category) in a single query.

    Scoped per sector because a best-available tier's pool is its sector narrowed to the
    requested zone — a venue-wide count for a category painted in two sectors would
    over-report for every tier that sells it (the exact confusion the picker was fixed for).

    The seat filter and the ``taken`` exclusion are the picker's own
    (:func:`events.service.seating.pick.load_candidates` /
    :func:`~events.service.seating.pick.load_taken_seats`), so the counts cannot promise
    seats a hold would not find. Accessible seats are tallied separately because
    ``pick_best_available`` scores a general request over non-accessible seats ONLY and
    never falls back — counting them as free would over-report by construction.

    Args:
        event: The event whose venue is summarized.
        taken: Unavailable seat ids, from ``load_taken_seats(event).union()``.

    Returns:
        One row per painted (sector, category), ordered by sector then category
        ``display_order``. Zones whose seats are all gone are reported with zeroes,
        never omitted.
    """
    if not event.venue_id:
        return []
    zones: dict[tuple[uuid.UUID, uuid.UUID], _ZoneAccumulator] = {}
    rows = VenueSeat.objects.filter(
        sector__venue_id=event.venue_id,
        sector__kind=VenueSector.Kind.SEATED,
        is_active=True,
        default_price_category__isnull=False,
    ).values_list(
        "id",
        "sector_id",
        "default_price_category_id",
        "row_order",
        "adjacency_index",
        "is_accessible",
        "sector__display_order",
        "default_price_category__display_order",
    )
    for seat_id, sector_id, category_id, row_order, adjacency_index, is_accessible, s_order, c_order in rows:
        zone = zones.setdefault((sector_id, category_id), _ZoneAccumulator(order=(s_order, c_order)))
        if seat_id in taken:
            continue
        if is_accessible:
            zone.accessible_free += 1
        else:
            zone.free += 1
            zone.rows.setdefault(row_order, []).append(adjacency_index)
    return [
        ZoneAvailabilitySchema(
            sector_id=sector_id,
            price_category_id=category_id,
            free_seats=zone.free,
            largest_contiguous_block=max((_longest_run(v) for v in zone.rows.values()), default=0),
            accessible_free_seats=zone.accessible_free,
        )
        for (sector_id, category_id), zone in sorted(
            zones.items(), key=lambda item: (item[1].order, str(item[0][0]), str(item[0][1]))
        )
    ]


def build_availability(event: Event, *, user: RevelUser | None, guest_session: str | None) -> SeatingAvailabilitySchema:
    """Build the sparse availability payload for one event's seated + standing sectors."""
    seats: dict[uuid.UUID, str] = {}

    # Shared with the picker so "unavailable here" and "unholdable there" can never diverge.
    # No hold owner: like the hold-acquisition path, EVERY active hold counts as taken —
    # including the caller's own, which `my_holds` reports separately.
    taken = load_taken_seats(event)
    for sid in taken.sold:
        seats[sid] = "sold"

    for sid in taken.blocked:
        seats.setdefault(sid, "blocked")

    if event.venue_id:
        inactive = VenueSeat.objects.filter(sector__venue_id=event.venue_id, is_active=False)
        for sid in inactive.values_list("id", flat=True):
            seats.setdefault(sid, "blocked")

    for sid in taken.held:
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
        zones=build_zone_availability(event, taken.union()),
        my_holds=my_holds,
        my_holds_expire_at=my_expiry,
        chart_updated_at=resolve_chart_version(event.venue_id) if event.venue_id else None,
    )
