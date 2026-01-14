# src/events/management/commands/bootstrap_helpers/venues.py
"""Venue creation for bootstrap process."""

import structlog

from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_venues(state: BootstrapState) -> None:
    """Create venues with sectors and seats for seated events."""
    logger.info("Creating venues...")

    # Create a venue for Revel Events Collective (org_alpha)
    concert_hall = events_models.Venue.objects.create(
        organization=state.orgs["alpha"],
        name="Revel Concert Hall",
        slug="revel-concert-hall",
        description="A modern concert venue with flexible seating arrangements.",
        city=state.cities["vienna"],
        address="Musikvereinsplatz 1, 1010 Vienna, Austria",
        capacity=100,
    )
    state.venues["concert_hall"] = concert_hall

    # Create a sector for the main floor
    main_floor = events_models.VenueSector.objects.create(
        venue=concert_hall,
        name="Main Floor",
        code="MF",
        capacity=100,
        display_order=1,
    )

    # Create 100 seats in a 10x10 grid
    # Rows A-J (10 rows), seats 1-10 per row
    # Positions are simple incrementals: (row, col) -> (0,0), (0,1)...(9,9)
    seats_to_create = []
    row_labels = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    for row_idx, row_label in enumerate(row_labels):
        for seat_num in range(1, 11):  # Seats 1-10
            col_idx = seat_num - 1
            seats_to_create.append(
                events_models.VenueSeat(
                    sector=main_floor,
                    label=f"{row_label}{seat_num}",
                    row=row_label,
                    number=seat_num,
                    position={"x": col_idx, "y": row_idx},
                    is_accessible=(row_label == "A"),  # First row is accessible
                    is_obstructed_view=False,
                    is_active=True,
                )
            )
    events_models.VenueSeat.objects.bulk_create(seats_to_create)

    logger.info(f"Created {len(state.venues)} venues with sectors and seats")
