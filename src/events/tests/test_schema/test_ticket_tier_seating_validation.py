"""Per-mode seating field validation on tier create/update schemas.

Both seated modes read the sector: USER_CHOICE assigns within it, BEST_AVAILABLE
draws its pool from it. A seated mode must arrive with the sector, or the tier
would pass validation yet be silently unsellable.

The ``# type: ignore[call-arg]`` comments cover fields with positional Field
defaults that pydantic-mypy reads as required (see engineering notes / #702).
"""

import uuid

import pytest
from pydantic import ValidationError

from events.models import TicketTier
from events.schema import TicketTierCreateSchema, TicketTierUpdateSchema


def test_create_best_available_without_sector_rejected() -> None:
    with pytest.raises(ValidationError, match="sector is required"):
        TicketTierCreateSchema(  # type: ignore[call-arg]
            name="BA",
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )


def test_create_user_choice_without_sector_rejected() -> None:
    with pytest.raises(ValidationError, match="sector is required"):
        TicketTierCreateSchema(  # type: ignore[call-arg]
            name="Seated",
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        )


def test_create_best_available_with_sector_valid() -> None:
    schema = TicketTierCreateSchema(  # type: ignore[call-arg]
        name="BA",
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        sector_id=uuid.uuid4(),
    )
    assert schema.sector_id is not None


def test_create_user_choice_with_sector_valid() -> None:
    schema = TicketTierCreateSchema(  # type: ignore[call-arg]
        name="Seated", seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE, sector_id=uuid.uuid4()
    )
    assert schema.sector_id is not None


def test_create_none_mode_requires_neither() -> None:
    schema = TicketTierCreateSchema(name="GA")  # type: ignore[call-arg]
    assert schema.seat_assignment_mode == TicketTier.SeatAssignmentMode.NONE


def test_update_best_available_without_sector_rejected() -> None:
    with pytest.raises(ValidationError, match="sector is required"):
        TicketTierUpdateSchema(  # type: ignore[call-arg]
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )


def test_update_best_available_with_sector_valid() -> None:
    schema = TicketTierUpdateSchema(  # type: ignore[call-arg]
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE, sector_id=uuid.uuid4()
    )
    assert schema.sector_id is not None


def test_update_user_choice_without_sector_rejected() -> None:
    with pytest.raises(ValidationError, match="sector is required"):
        TicketTierUpdateSchema(  # type: ignore[call-arg]
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE
        )


def test_update_without_mode_skips_seating_validation() -> None:
    schema = TicketTierUpdateSchema(name="Renamed")  # type: ignore[call-arg]
    assert schema.seat_assignment_mode is None
