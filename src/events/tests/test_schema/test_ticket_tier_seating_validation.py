"""Per-mode seating field validation on tier create/update schemas.

BEST_AVAILABLE reads the tier's price category; RANDOM/USER_CHOICE read the
sector — each mode must arrive with the field it reads, or the tier would pass
validation yet be silently unsellable.

The ``# type: ignore[call-arg]`` comments cover fields with positional Field
defaults that pydantic-mypy reads as required (see engineering notes / #702).
"""

import uuid

import pytest
from pydantic import ValidationError

from events.models import TicketTier
from events.schema import TicketTierCreateSchema, TicketTierUpdateSchema


def test_create_best_available_with_only_sector_rejected() -> None:
    with pytest.raises(ValidationError, match="price category is required"):
        TicketTierCreateSchema(  # type: ignore[call-arg]
            name="BA",
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            sector_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("mode", [TicketTier.SeatAssignmentMode.USER_CHOICE, TicketTier.SeatAssignmentMode.RANDOM])
def test_create_sector_modes_with_only_price_category_rejected(mode: TicketTier.SeatAssignmentMode) -> None:
    with pytest.raises(ValidationError, match="sector is required"):
        TicketTierCreateSchema(name="Seated", seat_assignment_mode=mode, price_category_id=uuid.uuid4())  # type: ignore[call-arg]


def test_create_best_available_with_price_category_valid() -> None:
    schema = TicketTierCreateSchema(  # type: ignore[call-arg]
        name="BA",
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        price_category_id=uuid.uuid4(),
    )
    assert schema.price_category_id is not None


@pytest.mark.parametrize("mode", [TicketTier.SeatAssignmentMode.USER_CHOICE, TicketTier.SeatAssignmentMode.RANDOM])
def test_create_sector_modes_with_sector_valid(mode: TicketTier.SeatAssignmentMode) -> None:
    schema = TicketTierCreateSchema(name="Seated", seat_assignment_mode=mode, sector_id=uuid.uuid4())  # type: ignore[call-arg]
    assert schema.sector_id is not None


def test_create_none_mode_requires_neither() -> None:
    schema = TicketTierCreateSchema(name="GA")  # type: ignore[call-arg]
    assert schema.seat_assignment_mode == TicketTier.SeatAssignmentMode.NONE


def test_update_best_available_with_only_sector_rejected() -> None:
    with pytest.raises(ValidationError, match="price category is required"):
        TicketTierUpdateSchema(  # type: ignore[call-arg]
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            sector_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("mode", [TicketTier.SeatAssignmentMode.USER_CHOICE, TicketTier.SeatAssignmentMode.RANDOM])
def test_update_sector_modes_with_only_price_category_rejected(mode: TicketTier.SeatAssignmentMode) -> None:
    with pytest.raises(ValidationError, match="sector is required"):
        TicketTierUpdateSchema(seat_assignment_mode=mode, price_category_id=uuid.uuid4())  # type: ignore[call-arg]


def test_update_without_mode_skips_seating_validation() -> None:
    schema = TicketTierUpdateSchema(name="Renamed")  # type: ignore[call-arg]
    assert schema.seat_assignment_mode is None
