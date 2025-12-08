"""Service layer for venue management operations."""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events import models, schema


def _convert_shape_to_coordinates(shape: list[dict[str, float]]) -> list[schema.Coordinate2D]:
    """Convert a JSON shape from DB to list of Coordinate2D objects.

    Args:
        shape: Shape data from database (list of dicts with x,y keys)

    Returns:
        List of Coordinate2D objects
    """
    return [schema.Coordinate2D(x=point["x"], y=point["y"]) for point in shape]


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

    Note:
        Seat position validation is handled by VenueSectorCreateSchema.
    """
    sector_data = payload.model_dump(exclude={"seats"})
    sector = models.VenueSector.objects.create(venue=venue, **sector_data)

    # Create seats if provided
    if payload.seats:
        seats_to_create = [models.VenueSeat(sector=sector, **seat.model_dump()) for seat in payload.seats]
        models.VenueSeat.objects.bulk_create(seats_to_create)

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
    """
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        return sector

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
        HttpError: If any seat position is outside the sector shape
    """
    if not seats:
        return []

    # Validate positions against sector shape if shape exists
    if sector.shape:
        shape_coords = _convert_shape_to_coordinates(sector.shape)
        _validate_seats_in_shape(seats, shape_coords)

    seats_to_create = [models.VenueSeat(sector=sector, **seat.model_dump()) for seat in seats]
    return list(models.VenueSeat.objects.bulk_create(seats_to_create))


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
    """
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        return seat

    # Validate position against sector shape if both are present
    if payload.position is not None and sector_shape is not None:
        shape_coords = _convert_shape_to_coordinates(sector_shape)
        if not schema.point_in_polygon(payload.position, shape_coords):
            raise HttpError(400, str(_("Seat position is outside the sector shape.")))

    for field, value in update_data.items():
        setattr(seat, field, value)

    seat.save(update_fields=list(update_data.keys()))
    return seat


def delete_seat(seat: models.VenueSeat) -> None:
    """Delete a seat.

    Args:
        seat: The seat to delete

    Raises:
        HttpError: If the seat has active or pending tickets for future events
    """
    # Check for active/pending tickets on future events
    blocking_ticket_exists = models.Ticket.objects.filter(
        seat=seat,
        status__in=[models.Ticket.TicketStatus.ACTIVE, models.Ticket.TicketStatus.PENDING],
        event__end__gt=timezone.now(),
    ).exists()

    if blocking_ticket_exists:
        raise HttpError(
            400,
            str(
                _("Cannot delete seat '{}' because it has active or pending tickets for future events.").format(
                    seat.label
                )
            ),
        )

    seat.delete()


@transaction.atomic
def bulk_delete_seats(sector: models.VenueSector, labels: list[str]) -> int:
    """Bulk delete seats by their labels.

    This operation is atomic - if any seat cannot be deleted (due to having
    active/pending tickets for future events), no seats will be deleted.

    Args:
        sector: The sector containing the seats
        labels: List of seat labels to delete

    Returns:
        The number of seats deleted

    Raises:
        HttpError: If any seat is not found or has blocking tickets
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

    # Check for blocking tickets on any of the seats
    blocking_seats = (
        models.Ticket.objects.filter(
            seat__in=seats,
            status__in=[models.Ticket.TicketStatus.ACTIVE, models.Ticket.TicketStatus.PENDING],
            event__end__gt=timezone.now(),
        )
        .values_list("seat__label", flat=True)
        .distinct()
    )
    blocking_labels = list(blocking_seats)

    if blocking_labels:
        raise HttpError(
            400,
            str(
                _("Cannot delete seats with active or pending tickets for future events: {}").format(
                    ", ".join(sorted(blocking_labels))
                )
            ),
        )

    # All validations passed, delete the seats
    deleted_count, _details = models.VenueSeat.objects.filter(sector=sector, label__in=labels).delete()
    return deleted_count


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
        HttpError: If any seat is not found or position is outside sector shape
    """
    if not updates:
        return []

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
        update_data = update.model_dump(exclude={"label"}, exclude_unset=True)

        if not update_data:
            updated_seats.append(seat)
            continue

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

        updated_seats.append(seat)

    # Bulk update if there are fields to update
    if update_fields:
        models.VenueSeat.objects.bulk_update(updated_seats, list(update_fields))

    return updated_seats
