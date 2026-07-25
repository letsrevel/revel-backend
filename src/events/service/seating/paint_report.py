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


class PaintReport(t.NamedTuple):
    """Everything a paint has to tell the organizer, in two deliberately separate lists.

    Kept apart because the populations are disjoint in both directions — see
    :class:`events.schema.UnsellableZoneTierSchema`.
    """

    affected_tiers: list[schema.AffectedTierSchema]
    unsellable_zone_tiers: list[schema.UnsellableZoneTierSchema]


def _tier_seat_price(price_map: dict[UUID, Decimal], category_id: UUID | None, flat_price: Decimal) -> Decimal | None:
    """What one seat in ``category_id`` costs on a category-priced tier, or ``None``.

    ``None`` is not "free": it means *this tier cannot sell that seat*. On a
    ``user_choice`` tier that is the reporting projection of
    :func:`events.service.seating.pricing.resolve_seat_price`'s refusal — a seat painted
    into a category the tier does not price has no honest price, and checkout returns a 400
    for it (spec §4.3). On a ``best_available`` tier the map keys *are* the tier's zones,
    so ``None`` reads as "outside this tier's pool" instead; either way the seat stopped
    (or started) being sellable at a price, which is what the report is advising about.
    Same convention as ``TierCategoryPriceSchema.price``.
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


def build_report(
    sector_ids: t.Collection[UUID],
    prior: t.Sequence[PriorPaint],
    new_category_id: UUID | None,
    painted_seat_ids: t.Collection[UUID],
) -> PaintReport:
    """The live category-priced tiers a paint repriced, under-covered, or left unfillable.

    Every other signal in the pricing system fires on the *absence* of a price: write-time
    validation, the checkout refusal, ``pricing_gaps``. Moving a seat between two categories
    the tier prices leaves coverage complete, so all of them stay silent while the seat's
    price changes for every event at the venue — ``paint_seats`` is venue-scoped. That silent
    case is what this reports; under-coverage is folded in as ``missing_categories`` rather
    than being the entry condition.

    The two halves have deliberately different tenses:

    - ``price_changes`` is the **delta of this paint** — what it just did to the money.
      Reported for **both** seated modes: since ``category_prices`` became the sole pricing
      mechanism (v3), a ``best_available`` tier reads the paint exactly like a
      ``user_choice`` one, so a repaint reprices its sales just as silently.
    - ``missing_categories`` is the tier's **current** gap, not the delta. A gap this paint
      did not open still leaves seats unsellable, and telling the admin "all clear" while
      checkout keeps refusing seats is worse than saying nothing. Reported for a *mapped*
      tier **only in user-choice**, for the same reason
      ``TicketTierDetailSchema.resolve_pricing_gaps`` and
      ``tier_pricing.validate_category_prices`` treat it that way: on a best-available tier
      the map keys *define* the sellable zones, so a painted category the map omits is not
      a gap — it is deliberately not part of this tier, and reporting it would be a
      permanent false alarm on every paint. An **empty-map** tier is a third case, reported
      in **both** modes: it charges its flat price for every seat in the sector, so a paint
      that leaves categories on that sector means premium seats sell at the flat price with
      nothing else to warn about it. Flat pricing on a painted sector stays legal (an
      organizer may paint for colour-coding alone), so this is advice, never a refusal.

    ``unsellable_zone_tiers`` is the **third** signal and the exact converse of the second:
    a best-available tier that prices a zone the sector no longer carries. Reported here and
    not only on the tier screen because the *cause* is a venue-screen action — unpainting (or
    repainting away) a zone's last seat — and the organizer is reading this response at the
    moment they take it. Kept in its own list rather than on ``affected_tiers``: an unpaint
    whose category was priced at the tier's flat price moves no money, so a tier can strand a
    zone without being "affected" at all. Its tense matches ``missing_categories`` — the
    tier's **current** unsellable zones, not this paint's delta, because a zone this paint did
    not strand still 409s every buyer who picks it, and going quiet about it on the next paint
    of the same sector would teach the organizer that silence means healthy. The condition is
    :func:`events.utils.tier_pricing.unsellable_zone_ids`, shared with
    ``TicketTierDetailSchema.resolve_unsellable_zones`` — including its guards, so a
    user-choice tier and a sector with no paint left on it both stay silent here too.

    Scope is otherwise deliberately narrow, because a warning that cries wolf gets ignored:
    only seated tiers read the paint at all, and only events that have not ended and are
    not cancelled — nobody can sell those seats anyway. DRAFT events stay in: the event
    being configured right now is the most valuable warning. A tier whose sector ends up
    with nothing painted reports nothing, in either mode.

    Query cost is constant in the number of seats painted: one query for the tiers, one for
    what is painted on the sectors, one to name the categories in both advisories.

    Args:
        sector_ids: The sectors that were touched.
        prior: The active seats' categories as captured *before* the UPDATE.
        new_category_id: The category just painted, or ``None`` for an unpaint.
        painted_seat_ids: The seats this paint writes. Excluded from the coverage read
            and replaced by ``new_category_id``, so the gap is computed the same way
            whether or not the UPDATE has run — which is what makes the dry run's
            answer identical to the real one rather than merely similar. The unsellable-zone
            half reads the *same* derived set, so it is preview-safe for the same reason —
            re-reading the sector after the UPDATE would have made the dry run lie about the
            very zone the admin is about to strand.

    Returns:
        Both advisory lists, each ordered by event start then tier name, and each empty when
        it has nothing to say — the common case.
    """
    if not sector_ids:
        return PaintReport([], [])

    tiers = list(
        models.TicketTier.objects.filter(
            seat_assignment_mode__in=(
                models.TicketTier.SeatAssignmentMode.USER_CHOICE,
                models.TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            ),
            sector_id__in=sector_ids,
            event__end__gte=timezone.now(),
        )
        .exclude(event__status=models.Event.EventStatus.CANCELLED)
        .select_related("event")
        .order_by("event__start", "name")
    )
    if not tiers:
        return PaintReport([], [])

    painted = _painted_after(sector_ids, prior, new_category_id, painted_seat_ids)
    prior_by_sector: dict[UUID, list[PriorPaint]] = {}
    for row in prior:
        prior_by_sector.setdefault(row.sector_id, []).append(row)

    entries: list[tuple[models.TicketTier, list[schema.SeatPriceChangeSchema], set[UUID]]] = []
    zone_entries: list[tuple[models.TicketTier, set[UUID]]] = []
    for tier in tiers:
        try:
            price_map = tier_pricing.parse_price_map(tier.category_prices)
        except DjangoValidationError:
            # A malformed legacy map must never turn a paint into an error (spec §4.3).
            continue
        # sector_id is non-null by the filter above; the guard is for the type checker.
        sector_id = tier.sector_id
        sector_painted: set[UUID] = painted.get(sector_id, set()) if sector_id else set()
        missing: set[UUID]
        changes: list[schema.SeatPriceChangeSchema]
        if not price_map:
            # Flat pricing over a painted sector: no seat's price moved (paint cannot move
            # a flat tier), but every painted category is being sold at `tier.price`.
            missing, changes = sector_painted, []
        else:
            # Best-available: the map keys are the tier's zones, so an unpriced painted
            # category is not a gap (same rule as `resolve_pricing_gaps`) — only user-choice
            # tiers can be under-covered.
            missing = (
                sector_painted - price_map.keys()
                if tier.seat_assignment_mode == models.TicketTier.SeatAssignmentMode.USER_CHOICE
                else set()
            )
            changes = _price_changes(
                prior_by_sector.get(sector_id, []) if sector_id else [],
                price_map,
                new_category_id,
                tier.price,
            )
        if changes or missing:
            entries.append((tier, changes, missing))
        # Independent of `entries`: a tier can strand a zone without this paint moving any
        # of its money. Derived from `sector_painted`, never a fresh read — that is what
        # keeps the preview byte-identical to the paint.
        unsellable = tier_pricing.unsellable_zone_ids(tier.seat_assignment_mode, price_map, sector_painted)
        if unsellable:
            zone_entries.append((tier, unsellable))
    if not entries and not zone_entries:
        return PaintReport([], [])

    # One query names the categories of both advisories.
    categories = {
        c.id: c
        for c in models.PriceCategory.objects.filter(
            id__in={cid for _tier, _changes, missing in entries for cid in missing}
            | {cid for _tier, unsellable in zone_entries for cid in unsellable}
        ).order_by("display_order", "name")
    }
    return PaintReport(
        affected_tiers=[
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
        ],
        unsellable_zone_tiers=[
            schema.UnsellableZoneTierSchema(
                tier_id=tier.id,
                tier_name=tier.name,
                event_id=tier.event_id,
                event_name=tier.event.name,
                event_status=models.Event.EventStatus(tier.event.status),
                zones=[
                    schema.TierUnsellableZoneSchema(id=c.id, name=c.name, color=c.color)
                    for cid, c in categories.items()
                    if cid in unsellable
                ],
            )
            for tier, unsellable in zone_entries
        ],
    )
