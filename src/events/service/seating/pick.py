"""DB-facing best-available: load candidates, score (pure core), hold winners optimistically."""

import typing as t
from uuid import UUID

from django.db.models import Max
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from events.exceptions import InvalidZoneSelectionError
from events.models import Event, EventSeatOverride, PriceCategory, SeatHold, Ticket, TicketTier, VenueSeat, VenueSector
from events.service.seating.best_available import CandidateSeat, pick_best_available
from events.service.seating.holds import HoldResult, acquire_seats
from events.utils.tier_pricing import parse_price_map

_MAX_ATTEMPTS = 3


def _zone_names(zone_ids: t.Iterable[UUID]) -> str:
    """Render a tier's sellable zones as a human list, for the 400 message.

    Falls back to a sentence when nothing resolves: a map key whose ``PriceCategory`` row
    was deleted leaves the tier with zones it cannot name, and "Select one of this ticket
    tier's zones: ." tells the buyer nothing and the support ticket even less.
    """
    names = list(
        PriceCategory.objects.filter(id__in=list(zone_ids))
        .order_by("display_order", "name")
        .values_list("name", flat=True)
    )
    if not names:
        return str(_("none are configured — please contact the organizer"))
    return ", ".join(names)


def resolve_requested_zone(tier: TicketTier, price_category_id: UUID | None) -> UUID | None:
    """Resolve the zone a best-available request draws from — the single authority.

    The zone is a REQUEST parameter, not a tier attribute: a tier with a non-empty
    ``category_prices`` map sells several differently-priced zones of its sector, and
    the buyer must say which one. Called by the hold route, authenticated checkout and
    guest checkout alike so the rule cannot drift between them.

    A supplied-but-unusable ``price_category_id`` is always an error, never a silent
    no-op: a parameter the buyer believes selected a zone, ignored, is a money bug.

    Args:
        tier: The tier being held/bought.
        price_category_id: The zone the buyer asked for, if any.

    Returns:
        The zone to narrow the pool to, or ``None`` for the tier's whole sector
        (flat pricing / non-best-available modes).

    Raises:
        InvalidZoneSelectionError: 400 — a zone is required but missing, is not one of
            the tier's zones (including a venue category this tier does not price), or
            was supplied where the tier cannot honour it.
    """
    if tier.seat_assignment_mode != TicketTier.SeatAssignmentMode.BEST_AVAILABLE:
        if price_category_id is not None:
            raise InvalidZoneSelectionError(str(_("A zone can only be selected on a best-available ticket tier.")))
        return None
    zone_ids = set(parse_price_map(tier.category_prices))
    if not zone_ids:
        if price_category_id is not None:
            raise InvalidZoneSelectionError(
                str(_("This ticket tier has a single price for its whole sector — no zone can be selected."))
            )
        return None
    if price_category_id not in zone_ids:
        raise InvalidZoneSelectionError(
            str(_("Select one of this ticket tier's zones: {zones}.")).format(zones=_zone_names(zone_ids))
        )
    return price_category_id


def load_candidates(
    event: Event,
    tier: TicketTier,
    exclude: set[t.Any],
    *,
    zone_id: UUID | None = None,
    hold_owner_user: RevelUser | None = None,
    hold_owner_guest_session: str | None = None,
) -> list[CandidateSeat]:
    """Load holdable seats in the tier's pool, excluding sold/held/blocked/inactive/lost.

    The pool is the tier's sector — never wider, so a price category painted across
    two sectors can't bleed one sector's seats into another's pool — narrowed to
    ``zone_id`` when the request selected one (see :func:`resolve_requested_zone`).

    When a hold-owner identity is given (purchase path), only FOREIGN active holds are
    excluded — the owner's own held seats remain candidates, to be consumed post-lock by
    ``verify_and_consume_holds``. With no identity (hold-acquisition path), ALL active
    holds are excluded.

    Returned in stable PK order so the seeded tiebreak in ``pick_best_available`` is
    reproducible across requests (and the re-pick after a conflict is deterministic).
    """
    if not tier.sector_id or event.venue_id is None:
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
    # Full-row bounds over ALL active seats of the pool's sector (sold/held included)
    # so centrality is scored against the real row midpoint, not the shrinking
    # available pool — and against the same sector the pool is drawn from.
    row_bounds = {
        (r["sector__display_order"], r["row_order"]): r["max_adjacency"] + 1
        for r in VenueSeat.objects.filter(
            is_active=True,
            sector__kind=VenueSector.Kind.SEATED,
            sector_id=tier.sector_id,
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
    if zone_id is not None:
        pool = pool.filter(default_price_category_id=zone_id)
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
    price_category_id: UUID | None = None,
) -> HoldResult:
    """Optimistic pick: read unlocked, score, hold only the winners; retry excluding losers.

    Returns an empty result (no held, no conflicts) when no block of ``quantity`` seats
    fits — the caller maps that to a 409.

    Raises:
        InvalidZoneSelectionError: 400 — see :func:`resolve_requested_zone`.
    """
    zone_id = resolve_requested_zone(tier, price_category_id)
    exclude: set[t.Any] = set()
    last = HoldResult(held=[], conflicts=[], expires_at=None)
    for _attempt in range(_MAX_ATTEMPTS):
        candidates = load_candidates(event, tier, exclude, zone_id=zone_id)
        picked = pick_best_available(candidates, quantity, accessible_required=accessible_required)
        if not picked:
            return HoldResult(held=[], conflicts=[], expires_at=None)
        last = acquire_seats(event, picked, user=user, guest_session=guest_session)
        if not last.conflicts:
            return last
        exclude |= set(last.conflicts)
    return last
