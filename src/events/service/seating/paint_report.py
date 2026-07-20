"""What a seat paint did to the money (#747) — read-only reporting, no writes.

Split out of ``venue_service`` verbatim; ``paint_seats`` is the only caller.
"""

import typing as t
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from events import models, schema
from events.utils import tier_pricing


class PriorPaint(t.NamedTuple):
    """How many active seats of one sector carried one category *before* a paint ran."""

    sector_id: UUID
    category_id: UUID | None
    seat_count: int


def _tier_seat_price(price_map: dict[UUID, Decimal], category_id: UUID | None, flat_price: Decimal) -> Decimal | None:
    """What one seat in ``category_id`` costs on a category-priced tier, or ``None``.

    ``None`` is not "free": it is the reporting projection of
    :func:`events.service.seating.pricing.resolve_seat_price`'s refusal — a seat painted
    into a category the tier does not price has no honest price, and checkout returns a 400
    for it (spec §4.3). Same convention as ``TierCategoryPriceSchema.price``.
    """
    if category_id is not None and category_id not in price_map:
        return None
    return tier_pricing.effective_category_price(price_map, category_id, flat_price)


def _price_changes(
    prior: t.Sequence[PriorPaint],
    price_map: dict[UUID, Decimal],
    new_category_id: UUID | None,
    flat_price: Decimal,
) -> list[schema.SeatPriceChangeSchema]:
    """Group one tier's repriced seats by the price they moved away from.

    A paint writes a single category, so ``to_price`` is one number per tier, but the seats
    it overwrote can have come from several — hence a list. Seats whose price is unchanged
    (a no-op repaint, or two categories priced the same) are omitted: reporting them would
    make the advisory fire on every paint and train the admin to dismiss it.
    """
    to_price = _tier_seat_price(price_map, new_category_id, flat_price)
    moved: dict[Decimal | None, int] = {}
    for row in prior:
        from_price = _tier_seat_price(price_map, row.category_id, flat_price)
        if from_price == to_price:
            continue
        moved[from_price] = moved.get(from_price, 0) + row.seat_count
    return [
        schema.SeatPriceChangeSchema(seat_count=count, from_price=from_price, to_price=to_price)
        # Biggest move first; ties broken by price so the payload is deterministic.
        for from_price, count in sorted(
            moved.items(), key=lambda kv: (-kv[1], kv[0] is None, kv[0] if kv[0] is not None else Decimal(0))
        )
    ]


def _painted_after(
    sector_ids: t.Collection[UUID],
    prior: t.Sequence[PriorPaint],
    new_category_id: UUID | None,
    painted_seat_ids: t.Collection[UUID],
) -> dict[UUID, set[UUID]]:
    """Which categories each touched sector carries once this paint has landed.

    Derived, not re-read: everything painted on the sector's *other* seats, plus the
    category this paint writes wherever it touches an active seat. That equals what a
    plain post-UPDATE read returns — and it equals it just as well before the UPDATE,
    which is what lets the dry run and the real paint share this line instead of each
    computing coverage its own way.
    """
    painted = tier_pricing.painted_categories_by_sector(sector_ids, exclude_seat_ids=painted_seat_ids)
    if new_category_id is not None:
        # `prior` holds active seats only, and only active seats count as painted.
        for row in prior:
            painted.setdefault(row.sector_id, set()).add(new_category_id)
    return painted


def affected_tiers(
    sector_ids: t.Collection[UUID],
    prior: t.Sequence[PriorPaint],
    new_category_id: UUID | None,
    painted_seat_ids: t.Collection[UUID],
) -> list[schema.AffectedTierSchema]:
    """The live category-priced tiers a paint repriced, under-covered, or both.

    Every other signal in the pricing system fires on the *absence* of a price: write-time
    validation, the checkout refusal, ``pricing_gaps``. Moving a seat between two categories
    the tier prices leaves coverage complete, so all of them stay silent while the seat's
    price changes for every event at the venue — ``paint_seats`` is venue-scoped. That silent
    case is what this reports; under-coverage is folded in as ``missing_categories`` rather
    than being the entry condition.

    The two halves have deliberately different tenses:

    - ``price_changes`` is the **delta of this paint** — what it just did to the money.
    - ``missing_categories`` is the tier's **current** gap, not the delta. A gap this paint
      did not open still leaves seats unsellable, and telling the admin "all clear" while
      checkout keeps refusing seats is worse than saying nothing.

    Scope is deliberately narrow, because a warning that cries wolf gets ignored: only
    ``USER_CHOICE`` tiers with a non-empty price map read the paint at all, and only events
    that have not ended and are not cancelled — nobody can sell those seats anyway. DRAFT
    events stay in: the event being configured right now is the most valuable warning.

    Query cost is constant in the number of seats painted: one query for the tiers, one for
    what is painted on the sectors, one to name the missing categories.

    Args:
        sector_ids: The sectors that were touched.
        prior: The active seats' categories as captured *before* the UPDATE.
        new_category_id: The category just painted, or ``None`` for an unpaint.
        painted_seat_ids: The seats this paint writes. Excluded from the coverage read
            and replaced by ``new_category_id``, so the gap is computed the same way
            whether or not the UPDATE has run — which is what makes the dry run's
            answer identical to the real one rather than merely similar.

    Returns:
        One entry per affected tier, ordered by event start then tier name. Empty when
        nothing is affected — the common case.
    """
    if not sector_ids:
        return []

    tiers = list(
        models.TicketTier.objects.filter(
            seat_assignment_mode=models.TicketTier.SeatAssignmentMode.USER_CHOICE,
            sector_id__in=sector_ids,
            event__end__gte=timezone.now(),
        )
        .exclude(event__status=models.Event.EventStatus.CANCELLED)
        .exclude(category_prices={})
        .select_related("event")
        .order_by("event__start", "name")
    )
    if not tiers:
        return []

    painted = _painted_after(sector_ids, prior, new_category_id, painted_seat_ids)
    prior_by_sector: dict[UUID, list[PriorPaint]] = {}
    for row in prior:
        prior_by_sector.setdefault(row.sector_id, []).append(row)

    entries: list[tuple[models.TicketTier, list[schema.SeatPriceChangeSchema], set[UUID]]] = []
    for tier in tiers:
        try:
            price_map = tier_pricing.parse_price_map(tier.category_prices)
        except DjangoValidationError:
            # A malformed legacy map must never turn a paint into an error (spec §4.3).
            continue
        if not price_map:
            # A map that parses to nothing is flat-priced at checkout; paint cannot move it.
            continue
        # sector_id is non-null by the filter above; the guard is for the type checker.
        sector_id = tier.sector_id
        missing = (painted.get(sector_id, set()) if sector_id else set()) - price_map.keys()
        changes = _price_changes(
            prior_by_sector.get(sector_id, []) if sector_id else [],
            price_map,
            new_category_id,
            tier.price,
        )
        if changes or missing:
            entries.append((tier, changes, missing))
    if not entries:
        return []

    categories = {
        c.id: c
        for c in models.PriceCategory.objects.filter(
            id__in={cid for _tier, _changes, missing in entries for cid in missing}
        ).order_by("display_order", "name")
    }
    return [
        schema.AffectedTierSchema(
            tier_id=tier.id,
            tier_name=tier.name,
            event_id=tier.event_id,
            event_name=tier.event.name,
            event_status=models.Event.EventStatus(tier.event.status),
            price_changes=changes,
            missing_categories=[
                schema.TierPricingGapSchema(id=c.id, name=c.name, color=c.color)
                for cid, c in categories.items()
                if cid in missing
            ],
        )
        for tier, changes, missing in entries
    ]
