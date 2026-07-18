"""Schemas for the seating engine (chart, availability, holds, overrides)."""

import typing as t
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, EmailStr, Field, field_serializer, field_validator, model_validator

from events.models import EventSeatOverride, TicketTier

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
    # "capacity" (caller holds too many seats) vs "unavailable" (seats taken/blocked)
    # vs "no_block" (best-available: no adjacent block of the requested size fits);
    # None on success.
    conflict_reason: str | None = None
    expires_at: AwareDatetime | None = None


class SeatOverrideItemSchema(Schema):
    seat_id: UUID
    status: EventSeatOverride.OverrideStatus
    reason: str = ""


class SeatOverridesRequest(Schema):
    set: list[SeatOverrideItemSchema] = Field(default_factory=list)
    release_seat_ids: list[UUID] = Field(default_factory=list)


class BoxOfficeSellRequest(Schema):
    """Door sale / comp: staff issues a ticket directly on a seat (spec §2)."""

    seat_id: UUID
    tier_id: UUID
    payment_method: TicketTier.PaymentMethod
    # Recipient: exactly one of email (guest get-or-create) or user_id (existing account).
    email: EmailStr | None = None
    user_id: UUID | None = None
    guest_name: str | None = Field(default=None, max_length=255)
    # Used only when a new guest user is created for the email.
    first_name: str = Field(default="", max_length=150)
    last_name: str = Field(default="", max_length=150)

    @field_validator("payment_method")
    @classmethod
    def _door_methods_only(cls, v: TicketTier.PaymentMethod) -> TicketTier.PaymentMethod:
        if v not in (TicketTier.PaymentMethod.AT_THE_DOOR, TicketTier.PaymentMethod.FREE):
            raise ValueError("payment_method must be 'at_the_door' or 'free'")
        return v

    @model_validator(mode="after")
    def _exactly_one_recipient(self) -> "BoxOfficeSellRequest":
        if (self.email is None) == (self.user_id is None):
            raise ValueError("Provide exactly one of 'email' or 'user_id'")
        return self


class BoxOfficeReseatRequest(Schema):
    """Move a ticket to another free seat in the same price category (spec §2)."""

    ticket_id: UUID
    target_seat_id: UUID


class SeatOverridesResponse(Schema):
    applied: int = 0
    released: int = 0
    rejected: dict[UUID, str] = Field(default_factory=dict)  # seat_id -> reason (e.g. "ticketed")

    # UUID dict keys aren't JSON-serializable (json.dumps rejects non-str keys) and Ninja
    # dumps responses in python mode, so stringify the keys at serialization time. The stored
    # attribute keeps UUID keys for in-process callers.
    @field_serializer("rejected")
    def _serialize_rejected(self, value: dict[UUID, str]) -> dict[str, str]:
        return {str(k): v for k, v in value.items()}
