"""DB-facing best-available: load candidates, score (pure core), hold winners optimistically."""

import typing as t

from django.db.models import Max

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, SeatHold, Ticket, TicketTier, VenueSeat, VenueSector
from events.service.seating.best_available import CandidateSeat, pick_best_available
from events.service.seating.holds import HoldResult, acquire_seats
from events.utils.tier_pricing import parse_price_map

_MAX_ATTEMPTS = 3


def load_candidates(
    event: Event,
    tier: TicketTier,
    exclude: set[t.Any],
    *,
    hold_owner_user: RevelUser | None = None,
    hold_owner_guest_session: str | None = None,
) -> list[CandidateSeat]:
    """Load holdable seats in the tier's pool, excluding sold/held/blocked/inactive/lost.

    The pool is the tier's sector, narrowed to the price categories its
    ``category_prices`` map names (an empty map = the whole sector).

    When a hold-owner identity is given (purchase path), only FOREIGN active holds are
    excluded — the owner's own held seats remain candidates, to be consumed post-lock by
    ``verify_and_consume_holds``. With no identity (hold-acquisition path), ALL active
    holds are excluded.

    Returned in stable PK order so the seeded tiebreak in ``pick_best_available`` is
    reproducible across requests (and the re-pick after a conflict is deterministic).
    """
    # ponytail: the zone is derived from the tier's map here. Threading the buyer's
    # requested `price_category_id` through as a per-request narrowing is Task B (#749).
    if not tier.sector_id or event.venue_id is None:
        return []
    zone_ids = list(parse_price_map(tier.category_prices))
    # Non-cancelled = occupied, matching the unique_ticket_event_seat constraint.
    taken = set(
        Ticket.objects.filter(event=event, seat__isnull=False)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("seat_id", flat=True)
    )
    holds_qs = SeatHold.objects.active().filter(event=event)
    if hold_owner_user is not None or hold_owner_guest_session is not None:
        holds_qs = holds_qs.exclude(SeatHold.owner_q(hold_owner_user, hold_owner_guest_session))
    taken |= set(holds_qs.values_list("seat_id", flat=True))
    taken |= set(EventSeatOverride.objects.filter(event=event).values_list("seat_id", flat=True))
    taken |= exclude
    # Full-row bounds over ALL active seats (sold/held included) so centrality is
    # scored against the real row midpoint, not the shrinking available pool.
    row_bounds = {
        (r["sector__display_order"], r["row_order"]): r["max_adjacency"] + 1
        for r in VenueSeat.objects.filter(
            is_active=True,
            sector__kind=VenueSector.Kind.SEATED,
            sector__venue_id=event.venue_id,
        )
        .values("sector__display_order", "row_order")
        .annotate(max_adjacency=Max("adjacency_index"))
    }
    pool = VenueSeat.objects.filter(
        sector_id=tier.sector_id,
        is_active=True,
        sector__kind=VenueSector.Kind.SEATED,
        sector__venue_id=event.venue_id,
    )
    if zone_ids:
        pool = pool.filter(default_price_category_id__in=zone_ids)
    qs = (
        pool.exclude(id__in=taken)
        .order_by("id")
        .values_list("id", "row_order", "adjacency_index", "is_accessible", "sector__display_order")
    )
    return [
        CandidateSeat(
            id=r[0],
            row_order=r[1],
            adjacency_index=r[2],
            is_accessible=r[3],
            sector_display_order=r[4],
            row_length=row_bounds[(r[4], r[1])],
        )
        for r in qs
    ]


def hold_best_available(
    event: Event,
    tier: TicketTier,
    quantity: int,
    *,
    user: RevelUser | None,
    guest_session: str | None,
    accessible_required: bool = False,
) -> HoldResult:
    """Optimistic pick: read unlocked, score, hold only the winners; retry excluding losers.

    Returns an empty result (no held, no conflicts) when no block of ``quantity`` seats
    fits — the caller maps that to a 409.
    """
    exclude: set[t.Any] = set()
    last = HoldResult(held=[], conflicts=[], expires_at=None)
    for _ in range(_MAX_ATTEMPTS):
        candidates = load_candidates(event, tier, exclude)
        picked = pick_best_available(candidates, quantity, accessible_required=accessible_required)
        if not picked:
            return HoldResult(held=[], conflicts=[], expires_at=None)
        last = acquire_seats(event, picked, user=user, guest_session=guest_session)
        if not last.conflicts:
            return last
        exclude |= set(last.conflicts)
    return last
