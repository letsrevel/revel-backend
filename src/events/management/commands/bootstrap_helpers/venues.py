# src/events/management/commands/bootstrap_helpers/venues.py
"""Venue creation for bootstrap process."""

import structlog

from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)

ORCHESTRA_ROWS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
ORCHESTRA_SEATS_PER_ROW = 12
ORCHESTRA_AISLES = [6]  # Center aisle, between seats 6 and 7
ORCHESTRA_PREMIUM_ROWS = ["A", "B", "C"]
ORCHESTRA_INACTIVE_LABELS = {"J6", "J7"}  # Two broken seats flanking the back-row aisle

BALCONY_ROWS = ["A", "B", "C", "D"]
BALCONY_SEATS_PER_ROW = 14
BALCONY_AISLES = [7]  # Center aisle, between seats 7 and 8


def _end_labels(row: str, seats_per_row: int, per_side: int = 2) -> set[str]:
    """Labels of the first and last ``per_side`` seats of ``row``."""
    labels: set[str] = set()
    for i in range(1, per_side + 1):
        labels.add(f"{row}{i}")
        labels.add(f"{row}{seats_per_row - i + 1}")
    return labels


def _build_grid_seats(
    sector: events_models.VenueSector,
    rows: list[str],
    seats_per_row: int,
    vertical_aisles: list[int],
    *,
    accessible: set[str] | None = None,
    obstructed: set[str] | None = None,
    inactive: set[str] | None = None,
    category_by_row: dict[str, events_models.PriceCategory] | None = None,
    default_category: events_models.PriceCategory | None = None,
) -> list[events_models.VenueSeat]:
    """Build seat instances for a rectangular grid with an optional aisle gap.

    Positions follow x = column index + one for every aisle at or before that
    column, so the aisle renders as a visible gap between seats.
    """
    accessible = accessible or set()
    obstructed = obstructed or set()
    inactive = inactive or set()
    category_by_row = category_by_row or {}

    seats: list[events_models.VenueSeat] = []
    for row_idx, row in enumerate(rows):
        category = category_by_row.get(row, default_category)
        for col in range(seats_per_row):
            x = col + sum(1 for aisle in vertical_aisles if aisle <= col)
            seat_num = col + 1
            label = f"{row}{seat_num}"
            seats.append(
                events_models.VenueSeat(
                    sector=sector,
                    label=label,
                    row_label=row,
                    number=seat_num,
                    default_price_category=category,
                    position={"x": x, "y": row_idx},
                    is_accessible=label in accessible,
                    is_obstructed_view=label in obstructed,
                    is_active=label not in inactive,
                    row_order=row_idx,
                    adjacency_index=col,
                )
            )
    return seats


def create_venues(state: BootstrapState) -> None:
    """Create the showcase venue: two sectors, price categories, and materialized seats."""
    logger.info("Creating venues...")

    concert_hall = events_models.Venue.objects.create(
        organization=state.orgs["alpha"],
        name="Revel Concert Hall",
        slug="revel-concert-hall",
        description="A modern concert venue with flexible seating arrangements.",
        city=state.cities["vienna"],
        address="Musikvereinsplatz 1, 1010 Vienna, Austria",
        capacity=176,
    )
    state.venues["concert_hall"] = concert_hall

    orchestra = events_models.VenueSector.objects.create(
        venue=concert_hall,
        name="Orchestra",
        code="ORC",
        capacity=120,
        display_order=1,
    )
    balcony = events_models.VenueSector.objects.create(
        venue=concert_hall,
        name="Balcony",
        code="BAL",
        capacity=56,
        display_order=2,
    )

    cat_orchestra_premium = events_models.PriceCategory.objects.create(
        venue=concert_hall, name="Orchestra Premium", color="#dc2626", display_order=0
    )
    cat_orchestra_standard = events_models.PriceCategory.objects.create(
        venue=concert_hall, name="Orchestra Standard", color="#f59e0b", display_order=1
    )
    cat_balcony = events_models.PriceCategory.objects.create(
        venue=concert_hall, name="Balcony", color="#7c3aed", display_order=2
    )

    seats = _build_grid_seats(
        orchestra,
        ORCHESTRA_ROWS,
        ORCHESTRA_SEATS_PER_ROW,
        ORCHESTRA_AISLES,
        accessible=_end_labels("A", ORCHESTRA_SEATS_PER_ROW),
        inactive=ORCHESTRA_INACTIVE_LABELS,
        category_by_row=dict.fromkeys(ORCHESTRA_PREMIUM_ROWS, cat_orchestra_premium),
        default_category=cat_orchestra_standard,
    )
    seats += _build_grid_seats(
        balcony,
        BALCONY_ROWS,
        BALCONY_SEATS_PER_ROW,
        BALCONY_AISLES,
        accessible=_end_labels("A", BALCONY_SEATS_PER_ROW),
        obstructed=_end_labels("D", BALCONY_SEATS_PER_ROW, per_side=1),
        default_category=cat_balcony,
    )
    events_models.VenueSeat.objects.bulk_create(seats)

    logger.info(f"Created 1 venue with {len(seats)} seats across 2 sectors")
