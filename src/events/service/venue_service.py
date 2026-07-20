"""Service layer for venue management operations."""

import re
import typing as t
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Case, Count, Exists, OuterRef, Q, Value, When
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events import models, schema
from events.utils import tier_pricing


def _seat_model_kwargs(data: dict[str, t.Any]) -> dict[str, t.Any]:
    """Map API seat fields onto model fields.

    - ``row`` → ``row_label`` (the deployed FE still sends/reads ``row``).
    - ``price_category_id`` → ``default_price_category_id``.
    - ``row_order`` / ``adjacency_index``: ``None`` means "derive server-side", so the
      keys are dropped (the model columns are non-nullable).

    Renames are conditional so ``exclude_unset`` update payloads stay untouched.
    """
    if "row" in data:
        data["row_label"] = data.pop("row")
    if "price_category_id" in data:
        data["default_price_category_id"] = data.pop("price_category_id")
    for rank_field in ("row_order", "adjacency_index"):
        if rank_field in data and data[rank_field] is None:
            del data[rank_field]
    return data


def _validate_seat_categories(venue_id: UUID, category_ids: set[UUID]) -> None:
    """Validate that every referenced price category belongs to the given venue.

    Args:
        venue_id: The venue the seats' sector belongs to
        category_ids: Price category ids referenced by the seat payloads (non-null)

    Raises:
        HttpError: 400 if any category does not belong to the venue
    """
    if not category_ids:
        return
    valid_ids = set(
        models.PriceCategory.objects.filter(venue_id=venue_id, id__in=category_ids).values_list("id", flat=True)
    )
    if category_ids - valid_ids:
        raise HttpError(400, str(_("Price category must belong to the same venue as the seats.")))


def natural_row_key(label: str) -> list[tuple[int, int] | tuple[int, int, str]]:
    """Natural sort key for row labels so front-to-back order is physically correct.

    Splits the label into alternating alpha/digit chunks and orders each chunk so:
    numeric chunks compare as integers (``"2"`` before ``"10"``), and alpha chunks
    compare length-first then lexicographically (``"Z"`` before ``"AA"`` — the theatre
    continuation scheme). Handles pure-numeric, pure-alpha, and mixed (``"A2"`` before
    ``"A10"``) labels. A plain string ``sorted()`` mis-orders all three.
    """
    key: list[tuple[int, int] | tuple[int, int, str]] = []
    for chunk in re.split(r"(\d+)", label):
        if not chunk:
            continue
        if chunk.isdigit():
            key.append((0, int(chunk)))
        else:
            key.append((1, len(chunk), chunk))
    return key


def derive_sector_seat_ranks(sector: models.VenueSector) -> None:
    """Re-rank the whole sector's seats (same semantics as migration 0098).

    ``row_order`` = dense rank of ``row_label`` (natural order, null rows in the
    0-bucket) per sector; ``adjacency_index`` = dense rank within the row —
    numbered seats first by ``(number, label)``, then null-numbered seats by
    ``label``. Row labels are ordered with :func:`natural_row_key` so numeric
    (``2`` before ``10``) and multi-letter (``Z`` before ``AA``) schemes rank
    front-to-back correctly. Re-ranking the whole sector keeps ranks consistent as
    seats are added or removed; it is cheap at realistic sector sizes (≤2,500 seats).
    """
    seats = list(sector.seats.all())
    row_labels = sorted({s.row_label for s in seats if s.row_label is not None}, key=natural_row_key)
    row_rank = {label: i for i, label in enumerate(row_labels)}
    by_row: dict[str | None, list[models.VenueSeat]] = {}
    for seat in seats:
        by_row.setdefault(seat.row_label, []).append(seat)
    to_update: list[models.VenueSeat] = []
    for label, row_seats in by_row.items():
        # numbered seats first (dense rank fixes 1,3,5… gaps), then null-numbered by label
        numbered = sorted((s for s in row_seats if s.number is not None), key=lambda s: (s.number, s.label))
        unnumbered = sorted((s for s in row_seats if s.number is None), key=lambda s: s.label)
        for idx, seat in enumerate(numbered + unnumbered):
            new_row_order = row_rank.get(label, 0) if label is not None else 0
            if seat.adjacency_index != idx or seat.row_order != new_row_order:
                seat.adjacency_index = idx
                seat.row_order = new_row_order
                to_update.append(seat)
    if to_update:
        models.VenueSeat.objects.bulk_update(to_update, ["adjacency_index", "row_order"], batch_size=500)


def _has_explicit_ranks(seats: t.Sequence[t.Any]) -> bool:
    """Whether any seat payload carries an explicit rank (explicit wins wholesale)."""
    return any(seat.row_order is not None or seat.adjacency_index is not None for seat in seats)


def _convert_shape_to_coordinates(shape: list[t.Any]) -> list[schema.Coordinate2D]:
    """Convert a JSON shape from DB to list of Coordinate2D objects.

    Args:
        shape: Shape data from database — canonical ``{"x": .., "y": ..}`` dicts,
            or legacy ``[x, y]`` pairs (coerced by Coordinate2D validation).

    Returns:
        List of Coordinate2D objects
    """
    return [schema.Coordinate2D.model_validate(point) for point in shape]


def create_venue(
    organization: models.Organization,
    payload: schema.VenueCreateSchema,
) -> models.Venue:
    """Create a new venue for an organization.

    Args:
        organization: The organization to create the venue for
        payload: The venue creation data

    Returns:
        The created venue
    """
    venue = models.Venue.objects.create(
        organization=organization,
        **payload.model_dump(),
    )
    return venue


@transaction.atomic
def update_venue(
    venue: models.Venue,
    payload: schema.VenueUpdateSchema,
) -> models.Venue:
    """Update a venue.

    Args:
        venue: The venue to update
        payload: The venue update data

    Returns:
        The updated venue
    """
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        return venue

    for field, value in update_data.items():
        setattr(venue, field, value)

    venue.save()
    return venue


def create_price_category(
    venue: models.Venue,
    payload: schema.PriceCategoryCreateSchema,
) -> models.PriceCategory:
    """Create a price category for a venue.

    A duplicate ``(venue, name)`` surfaces as a Django ``ValidationError``
    (the model's ``save()`` runs ``full_clean()``), which the global handler
    renders as a 400 — same contract as venue creation.

    Args:
        venue: The venue to create the category for
        payload: The category creation data

    Returns:
        The created price category
    """
    return models.PriceCategory.objects.create(venue=venue, **payload.model_dump())


@transaction.atomic
def update_price_category(
    category: models.PriceCategory,
    payload: schema.PriceCategoryUpdateSchema,
) -> models.PriceCategory:
    """Update a price category.

    Args:
        category: The price category to update
        payload: The category update data (partial)

    Returns:
        The updated price category
    """
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        return category

    for field, value in update_data.items():
        setattr(category, field, value)

    category.save(update_fields=list(update_data.keys()))
    return category


def delete_price_category(category: models.PriceCategory) -> None:
    """Delete a price category.

    Refuses deletion when any ticket tier references the category, in either of the
    two ways a tier can:

    - the direct ``price_category`` FK (BEST_AVAILABLE tiers) — ``SET_NULL``, so a
      delete would silently strip the category and leave the tier unsellable;
    - the ``category_prices`` JSON map (USER_CHOICE tiers) — invisible to the
      database, so **this guard is the only line of defence**. Deleting a category
      priced by a live tier would unpaint its seats (``SET_NULL``) and silently
      collapse those seats back to the tier's flat ``price``: an €80 premium seat
      sold at €50, with nothing anywhere reporting it.

    Seats painted with a category no tier prices are fine — their
    ``default_price_category`` becomes NULL and they can simply be repainted.

    Args:
        category: The price category to delete

    Raises:
        HttpError: If any ticket tier references the category
    """
    blocking = (
        models.TicketTier.objects.filter(Q(price_category=category) | Q(category_prices__has_key=str(category.id)))
        .select_related("event")
        .order_by("event__name", "name")
    )
    # Name the offenders: a category is venue-scoped, so the tiers holding it can belong
    # to any number of events and an admin cannot otherwise find them.
    labels = [f"{tier.event.name} — {tier.name}" for tier in blocking]
    if labels:
        raise HttpError(
            400,
            str(
                _(
                    "This price category is used by one or more ticket tiers and cannot be deleted: {tiers}. "
                    "Reassign those tiers, or remove the category from their category prices, first."
                )
            ).format(tiers="; ".join(labels)),
        )

    category.delete()


@transaction.atomic
def create_sector(
    venue: models.Venue,
    payload: schema.VenueSectorCreateSchema,
) -> models.VenueSector:
    """Create a new sector for a venue with optional nested seats.

    Args:
        venue: The venue to create the sector for
        payload: The sector creation data including optional seats

    Returns:
        The created sector with its seats

    Raises:
        HttpError: If any seat references a price category of another venue

    Note:
        Seat position validation is handled by VenueSectorCreateSchema.
        Unless any seat carries an explicit ``row_order``/``adjacency_index``
        (explicit wins wholesale), both ranks are derived for the sector.
    """
    sector_data = payload.model_dump(exclude={"seats"})
    sector = models.VenueSector.objects.create(venue=venue, **sector_data)

    # Create seats if provided
    if payload.seats:
        _validate_seat_categories(
            venue.id, {s.price_category_id for s in payload.seats if s.price_category_id is not None}
        )
        seats_to_create = [
            models.VenueSeat(sector=sector, **_seat_model_kwargs(seat.model_dump())) for seat in payload.seats
        ]
        models.VenueSeat.objects.bulk_create(seats_to_create)
        if not _has_explicit_ranks(payload.seats):
            derive_sector_seat_ranks(sector)

    return sector


def _get_shape_coords(
    payload_shape: list[schema.Coordinate2D] | None,
    db_shape: list[dict[str, float]] | None,
) -> list[schema.Coordinate2D] | None:
    """Get shape coordinates from payload or DB."""
    if payload_shape is not None:
        return payload_shape
    if db_shape is not None:
        return _convert_shape_to_coordinates(db_shape)
    return None


def _validate_seats_in_shape(
    seats: list[schema.VenueSeatInputSchema],
    shape: list[schema.Coordinate2D],
) -> None:
    """Validate that all seat positions are within the shape polygon."""
    for seat in seats:
        if seat.position is not None and not schema.point_in_polygon(seat.position, shape):
            raise HttpError(
                400,
                str(_("Seat '{}' position is outside the sector shape.").format(seat.label)),
            )


@transaction.atomic
def update_sector(
    sector: models.VenueSector,
    payload: schema.VenueSectorUpdateSchema,
) -> models.VenueSector:
    """Update a sector's metadata.

    Args:
        sector: The sector to update
        payload: The sector update data

    Returns:
        The updated sector

    Raises:
        HttpError: If ``kind`` would change while the sector has seats
    """
    update_data = payload.model_dump(exclude_unset=True)
    if update_data.get("kind") is None:
        update_data.pop("kind", None)
    if not update_data:
        return sector

    if "kind" in update_data and update_data["kind"] != sector.kind and sector.seats.exists():
        raise HttpError(
            400,
            str(_("Sector kind can only be changed while the sector has no seats. Delete its seats first.")),
        )

    for field, value in update_data.items():
        setattr(sector, field, value)

    sector.save(update_fields=list(update_data.keys()))
    return sector


@transaction.atomic
def bulk_create_seats(
    sector: models.VenueSector,
    seats: list[schema.VenueSeatInputSchema],
) -> list[models.VenueSeat]:
    """Bulk create seats for a sector.

    Args:
        sector: The sector to add seats to
        seats: List of seat data to create

    Returns:
        The created seats

    Raises:
        HttpError: If any seat position is outside the sector shape, or any seat
            references a price category of another venue

    Note:
        Unless any seat carries an explicit ``row_order``/``adjacency_index``
        (explicit wins wholesale), both ranks are re-derived for the whole sector.
    """
    if not seats:
        return []

    # Validate positions against sector shape if shape exists
    if sector.shape:
        shape_coords = _convert_shape_to_coordinates(sector.shape)
        _validate_seats_in_shape(seats, shape_coords)

    _validate_seat_categories(sector.venue_id, {s.price_category_id for s in seats if s.price_category_id is not None})

    seats_to_create = [models.VenueSeat(sector=sector, **_seat_model_kwargs(seat.model_dump())) for seat in seats]
    created = list(models.VenueSeat.objects.bulk_create(seats_to_create))
    if not _has_explicit_ranks(seats):
        derive_sector_seat_ranks(sector)
    # Refetch with the painted category so the VenueSeatSchema response has it without an N+1
    # (also picks up any derived row_order/adjacency_index). Order preserved via `created`.
    by_id = models.VenueSeat.objects.select_related("default_price_category").in_bulk([s.id for s in created])
    return [by_id[s.id] for s in created]


def get_seat_by_label(sector: models.VenueSector, label: str) -> models.VenueSeat:
    """Get a seat by its label within a sector.

    Args:
        sector: The sector containing the seat
        label: The seat label

    Returns:
        The seat

    Raises:
        HttpError: If the seat is not found
    """
    try:
        return sector.seats.get(label=label)
    except models.VenueSeat.DoesNotExist:
        raise HttpError(404, str(_("Seat with label '{}' not found in this sector.").format(label)))


@transaction.atomic
def update_seat(
    seat: models.VenueSeat,
    payload: schema.VenueSeatUpdateSchema,
    sector_shape: list[dict[str, float]] | None = None,
) -> models.VenueSeat:
    """Update a seat.

    Args:
        seat: The seat to update
        payload: The seat update data
        sector_shape: The sector shape for position validation (raw JSON from DB)

    Returns:
        The updated seat

    Note:
        When the update touches ``row``/``number`` without explicit
        ``row_order``/``adjacency_index`` (explicit wins wholesale), both ranks
        are re-derived for the whole sector.
    """
    update_data = _seat_model_kwargs(payload.model_dump(exclude_unset=True))
    if not update_data:
        return seat

    # Validate position against sector shape if both are present
    if payload.position is not None and sector_shape is not None:
        shape_coords = _convert_shape_to_coordinates(sector_shape)
        if not schema.point_in_polygon(payload.position, shape_coords):
            raise HttpError(400, str(_("Seat position is outside the sector shape.")))

    if payload.price_category_id is not None:
        _validate_seat_categories(seat.sector.venue_id, {payload.price_category_id})

    for field, value in update_data.items():
        setattr(seat, field, value)

    seat.save(update_fields=list(update_data.keys()))

    if not _has_explicit_ranks([payload]) and ({"row_label", "number"} & set(update_data)):
        derive_sector_seat_ranks(seat.sector)
        seat.refresh_from_db(fields=["row_order", "adjacency_index"])

    return seat


def delete_seat(seat: models.VenueSeat) -> None:
    """Delete a seat.

    Args:
        seat: The seat to delete

    Raises:
        HttpError: If the seat is referenced by any ticket (any status/event, ever)
            or has an unexpired hold
    """
    # A seat referenced by any ticket, ever, or held right now, is never hard-deleted.
    blocking_ticket_exists = models.Ticket.objects.filter(seat=seat).exists()
    blocking_hold_exists = models.SeatHold.objects.active().filter(seat=seat).exists()

    if blocking_ticket_exists or blocking_hold_exists:
        raise HttpError(
            400,
            str(
                _(
                    "Seat '{}' is referenced by tickets or an active hold and cannot be deleted. "
                    "Decommission it instead (set inactive)."
                ).format(seat.label)
            ),
        )

    seat.delete()


@transaction.atomic
def bulk_delete_seats(sector: models.VenueSector, labels: list[str]) -> int:
    """Bulk delete seats by their labels.

    This operation is atomic - if any seat cannot be deleted (because it is
    referenced by any ticket, ever, or has an unexpired hold), no seats will be deleted.

    Args:
        sector: The sector containing the seats
        labels: List of seat labels to delete

    Returns:
        The number of seats deleted

    Raises:
        HttpError: If any seat is not found or is referenced by tickets/holds
    """
    if not labels:
        return 0

    # First, verify all seats exist
    seats = list(sector.seats.filter(label__in=labels))
    found_labels = {seat.label for seat in seats}
    missing_labels = set(labels) - found_labels

    if missing_labels:
        raise HttpError(
            404,
            str(_("Seats not found in this sector: {}").format(", ".join(sorted(missing_labels)))),
        )

    # A seat referenced by any ticket, ever, or held right now, is never hard-deleted.
    blocking_ticket_labels = set(
        models.Ticket.objects.filter(seat__in=seats).values_list("seat__label", flat=True).distinct()
    )
    blocking_hold_labels = set(
        models.SeatHold.objects.active().filter(seat__in=seats).values_list("seat__label", flat=True).distinct()
    )
    blocking_labels = blocking_ticket_labels | blocking_hold_labels

    if blocking_labels:
        raise HttpError(
            400,
            str(
                _(
                    "Seats referenced by tickets or active holds cannot be deleted: {}. "
                    "Decommission them instead (set inactive)."
                ).format(", ".join(sorted(blocking_labels)))
            ),
        )

    # All validations passed, delete the seats
    deleted_count, _details = models.VenueSeat.objects.filter(sector=sector, label__in=labels).delete()
    return deleted_count


def _apply_seat_update(
    seat: models.VenueSeat,
    update: schema.VenueSeatBulkUpdateItemSchema,
    shape_coords: list[schema.Coordinate2D] | None,
    update_fields: set[str],
) -> None:
    """Apply one bulk-update item to a seat in memory, tracking the touched fields."""
    update_data = _seat_model_kwargs(update.model_dump(exclude={"label"}, exclude_unset=True))
    if not update_data:
        return

    # Validate position against sector shape if both are present
    if update.position is not None and shape_coords is not None:
        if not schema.point_in_polygon(update.position, shape_coords):
            raise HttpError(
                400,
                str(_("Seat '{}' position is outside the sector shape.").format(update.label)),
            )

    for field, value in update_data.items():
        setattr(seat, field, value)
        update_fields.add(field)


@transaction.atomic
def bulk_update_seats(
    sector: models.VenueSector,
    updates: list[schema.VenueSeatBulkUpdateItemSchema],
) -> list[models.VenueSeat]:
    """Bulk update seats in a sector.

    This operation is atomic - if any seat cannot be updated, no seats will be updated.

    Args:
        sector: The sector containing the seats
        updates: List of seat update items with label as identifier

    Returns:
        The list of updated seats

    Raises:
        HttpError: If any seat is not found, a position is outside the sector
            shape, or a price category belongs to another venue

    Note:
        When any update touches ``row``/``number`` and no seat in the request
        carries an explicit ``row_order``/``adjacency_index`` (explicit wins
        wholesale), both ranks are re-derived for the whole sector.
    """
    if not updates:
        return []

    _validate_seat_categories(
        sector.venue_id, {u.price_category_id for u in updates if u.price_category_id is not None}
    )

    # Extract labels and verify all seats exist
    labels = [update.label for update in updates]
    seats = list(sector.seats.filter(label__in=labels))
    seats_by_label = {seat.label: seat for seat in seats}

    found_labels = set(seats_by_label.keys())
    missing_labels = set(labels) - found_labels

    if missing_labels:
        raise HttpError(
            404,
            str(_("Seats not found in this sector: {}").format(", ".join(sorted(missing_labels)))),
        )

    # Get sector shape for position validation
    shape_coords: list[schema.Coordinate2D] | None = None
    if sector.shape:
        shape_coords = _convert_shape_to_coordinates(sector.shape)

    # Process each update
    updated_seats: list[models.VenueSeat] = []
    update_fields: set[str] = set()

    for update in updates:
        seat = seats_by_label[update.label]
        _apply_seat_update(seat, update, shape_coords, update_fields)
        updated_seats.append(seat)

    # Bulk update if there are fields to update. bulk_update bypasses auto_now, so stamp
    # updated_at by hand — it is what the chart version (and therefore the buyer's poller)
    # is derived from.
    if update_fields:
        now = timezone.now()
        for seat in updated_seats:
            seat.updated_at = now
        models.VenueSeat.objects.bulk_update(updated_seats, [*update_fields, "updated_at"])

    if not _has_explicit_ranks(updates) and ({"row_label", "number"} & update_fields):
        derive_sector_seat_ranks(sector)

    # Refetch with the painted category so the VenueSeatSchema response has it without an N+1
    # (also picks up any derived row_order/adjacency_index). Order preserved via `updated_seats`.
    by_id = models.VenueSeat.objects.select_related("default_price_category").in_bulk([s.id for s in updated_seats])
    return [by_id[s.id] for s in updated_seats]


class PriorPaint(t.NamedTuple):
    """How many active seats of one sector carried one category *before* a paint ran."""

    sector_id: UUID
    category_id: UUID | None
    seat_count: int


@transaction.atomic
def paint_seats(venue: models.Venue, payload: schema.VenueSeatPaintSchema) -> schema.SeatPaintResultSchema:
    """Bulk paint (or unpaint) seats with a price category in a single UPDATE.

    Painting always succeeds (spec §4.3): a venue-wide map operation must never be
    blocked by one event's pricing config. The consequence is reported instead —
    see :func:`affected_tiers`.

    Args:
        venue: The venue the seats and the category must belong to
        payload: Seat ids and the category to paint (null = unpaint)

    Returns:
        The number of seats painted, plus every live category-priced tier whose seat
        prices this changed or whose sector it left partly unsellable.

    Raises:
        HttpError: 400 if the category belongs to another venue, 404 if any seat
            does not belong to this venue
    """
    if payload.price_category_id is not None:
        _validate_seat_categories(venue.id, {payload.price_category_id})

    seat_ids = set(payload.seat_ids)
    seats = models.VenueSeat.objects.filter(id__in=seat_ids, sector__venue=venue)
    # One grouped read serves all three pre-UPDATE needs — the 404 check, which sectors were
    # touched, and the categories the seats carried *before* the UPDATE overwrites them.
    # Grouped, not per-seat: its cost is bounded by (sector × category × is_active), never by
    # how many seats are painted. `.order_by()` clears the model's Meta ordering, which would
    # otherwise leak into the GROUP BY.
    prior_rows = list(
        seats.values("sector_id", "default_price_category_id", "is_active").order_by().annotate(seat_count=Count("id"))
    )
    if sum(row["seat_count"] for row in prior_rows) != len(seat_ids):
        raise HttpError(404, str(_("Some seats were not found in this venue.")))
    sector_ids = {row["sector_id"] for row in prior_rows}
    # Only active seats can be sold, so only they can be repriced — the same rule
    # `tier_pricing._painted_seats` applies to coverage.
    prior = [
        PriorPaint(row["sector_id"], row["default_price_category_id"], row["seat_count"])
        for row in prior_rows
        if row["is_active"]
    ]

    # A queryset .update() bypasses auto_now, which would leave the chart version unchanged
    # after a repaint — and a repaint now changes what buyers are charged, so the poller has
    # to see it.
    painted = seats.update(default_price_category_id=payload.price_category_id, updated_at=timezone.now())
    return schema.SeatPaintResultSchema(
        painted=painted,
        affected_tiers=affected_tiers(sector_ids, prior, payload.price_category_id),
    )


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


def affected_tiers(
    sector_ids: t.Collection[UUID],
    prior: t.Sequence[PriorPaint],
    new_category_id: UUID | None,
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

    painted = tier_pricing.painted_categories_by_sector(sector_ids)
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


def get_tier_seat_availability(
    event: models.Event,
    tier: models.TicketTier,
) -> schema.SectorAvailabilitySchema:
    """Get seat availability for a ticket tier with seat assignment.

    Returns sector info with all seats and their availability status.
    Seats taken by any non-cancelled ticket are marked as available=False.
    Serves any seat-assigned mode (USER_CHOICE or BEST_AVAILABLE),
    provided the tier has a sector assigned.

    Args:
        event: The event to check availability for
        tier: The ticket tier (must have sector assigned and seat_assignment_mode != NONE)

    Returns:
        SectorAvailabilitySchema with seats and availability counts

    Raises:
        HttpError: If tier doesn't have seat assignment or no sector is assigned
    """
    # Validate tier has seat assignment
    if tier.seat_assignment_mode == models.TicketTier.SeatAssignmentMode.NONE:
        raise HttpError(404, str(_("This tier does not have seat assignment.")))

    if not tier.sector_id:
        raise HttpError(404, str(_("This tier does not have an assigned sector.")))

    # Get sector
    sector = models.VenueSector.objects.get(pk=tier.sector_id)

    # Subquery to check if a seat is taken. Occupancy matches the
    # unique_ticket_event_seat constraint: any non-cancelled ticket
    # (incl. CHECKED_IN) occupies the seat.
    taken_ticket_exists = models.Ticket.objects.filter(
        event=event,
        seat_id=OuterRef("pk"),
    ).exclude(status=models.Ticket.TicketStatus.CANCELLED)

    # Annotate seats with availability status
    seats_with_availability = (
        models.VenueSeat.objects.filter(sector=sector, is_active=True)
        .annotate(
            available=Case(
                When(Exists(taken_ticket_exists), then=Value(False)),
                default=Value(True),
            )
        )
        .order_by("row_label", "number", "label")
    )

    # Build response with availability counts
    seats: list[schema.VenueSeatSchema] = []
    available_count = 0

    for seat in seats_with_availability:
        seat_schema = schema.VenueSeatSchema.from_orm(seat)
        seat_schema.available = seat.available
        seats.append(seat_schema)
        if seat.available:
            available_count += 1

    # Convert shape to Coordinate2D if present
    shape: list[schema.Coordinate2D] | None = None
    if sector.shape:
        shape = _convert_shape_to_coordinates(sector.shape)

    return schema.SectorAvailabilitySchema(
        id=sector.id,
        name=sector.name,
        code=sector.code,
        shape=shape,
        capacity=sector.capacity,
        display_order=sector.display_order,
        metadata=sector.metadata,
        seats=seats,
        available_count=available_count,
        total_count=len(seats),
    )
