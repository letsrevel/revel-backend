"""Schemas for the seating engine (chart, availability, holds, overrides)."""

import typing as t
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field, field_serializer

from events.models import EventSeatOverride

from .venue import Coordinate2D, PriceCategorySchema


class ChartSeatSchema(Schema):
    id: UUID
    label: str
    row_label: str | None = None
    row_order: int = 0
    number: int | None = None
    adjacency_index: int = 0
    position: Coordinate2D | None = None
    is_accessible: bool = False
    is_obstructed_view: bool = False
    is_active: bool = True
    price_category_id: UUID | None = None


class ChartSectorSchema(Schema):
    id: UUID
    name: str
    code: str | None = None
    kind: str = "seated"
    shape: list[Coordinate2D] | None = None
    capacity: int | None = None
    display_order: int = 0
    metadata: dict[str, t.Any] | None = None
    seats: list[ChartSeatSchema] = Field(default_factory=list)


class VenueChartSchema(Schema):
    venue_id: UUID
    venue_name: str
    updated_at: AwareDatetime
    price_categories: list[PriceCategorySchema] = Field(default_factory=list)
    sectors: list[ChartSectorSchema] = Field(default_factory=list)


class StandingAvailabilitySchema(Schema):
    capacity: int | None = None
    taken: int = 0


class SeatingAvailabilitySchema(Schema):
    seats: dict[UUID, str] = Field(default_factory=dict)  # sparse: seat_id -> sold|held|blocked
    standing: dict[UUID, StandingAvailabilitySchema] = Field(default_factory=dict)
    my_holds: list[UUID] = Field(default_factory=list)
    my_holds_expire_at: AwareDatetime | None = None

    # UUID dict keys aren't JSON-serializable (json.dumps rejects non-str keys) and Ninja
    # dumps responses in python mode, so stringify the keys at serialization time. The stored
    # attributes keep UUID keys for in-process callers.
    @field_serializer("seats")
    def _serialize_seats(self, value: dict[UUID, str]) -> dict[str, str]:
        return {str(k): v for k, v in value.items()}

    @field_serializer("standing")
    def _serialize_standing(self, value: dict[UUID, StandingAvailabilitySchema]) -> dict[str, dict[str, t.Any]]:
        return {str(k): v.model_dump() for k, v in value.items()}


class HoldSeatsRequest(Schema):
    seat_ids: list[UUID] = Field(default_factory=list, max_length=50)


class BestAvailableHoldRequest(Schema):
    tier_id: UUID
    quantity: int = Field(..., ge=1, le=20)
    accessible_required: bool = False


class ReleaseSeatsRequest(Schema):
    seat_ids: list[UUID] | None = None  # None = release all


class HoldResponseSchema(Schema):
    held_seat_ids: list[UUID] = Field(default_factory=list)
    conflicts: list[UUID] = Field(default_factory=list)
    expires_at: AwareDatetime | None = None


class SeatOverrideItemSchema(Schema):
    seat_id: UUID
    status: EventSeatOverride.OverrideStatus
    reason: str = ""


class SeatOverridesRequest(Schema):
    set: list[SeatOverrideItemSchema] = Field(default_factory=list)
    release_seat_ids: list[UUID] = Field(default_factory=list)


class SeatOverridesResponse(Schema):
    applied: int = 0
    released: int = 0
    rejected: dict[UUID, str] = Field(default_factory=dict)  # seat_id -> reason (e.g. "ticketed")
