"""DB-facing best-available: load candidates, score (pure core), hold winners optimistically."""

import typing as t

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, SeatHold, Ticket, TicketTier, VenueSeat, VenueSector
from events.service.seating.best_available import CandidateSeat, pick_best_available
from events.service.seating.holds import HoldResult, acquire_seats

_MAX_ATTEMPTS = 3


def load_candidates(
    event: Event,
    tier: TicketTier,
    exclude: set[t.Any],
    *,
    hold_owner_user: RevelUser | None = None,
    hold_owner_guest_session: str | None = None,
) -> list[CandidateSeat]:
    """Load holdable seats in the tier's price category, excluding sold/held/blocked/inactive/lost.

    When a hold-owner identity is given (purchase path), only FOREIGN active holds are
    excluded — the owner's own held seats remain candidates, to be consumed post-lock by
    ``verify_and_consume_holds``. With no identity (hold-acquisition path), ALL active
    holds are excluded.

    Returned in stable PK order so the seeded tiebreak in ``pick_best_available`` is
    reproducible across requests (and the re-pick after a conflict is deterministic).
    """
    if not tier.price_category_id or event.venue_id is None:
        return []
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
    qs = (
        VenueSeat.objects.filter(
            default_price_category_id=tier.price_category_id,
            is_active=True,
            sector__kind=VenueSector.Kind.SEATED,
            sector__venue_id=event.venue_id,
        )
        .exclude(id__in=taken)
        .order_by("id")
        .values_list("id", "row_order", "adjacency_index", "is_accessible", "sector__display_order")
    )
    return [
        CandidateSeat(id=r[0], row_order=r[1], adjacency_index=r[2], is_accessible=r[3], sector_display_order=r[4])
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
