"""Venue-related schemas."""

import typing as t
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import Field, StringConstraints, model_validator

from common.schema import OneToOneFiftyString, StrippedString
from events.models import Venue, VenueSeat, VenueSector

from .mixins import CityEditMixin, CityRetrieveMixin


class Coordinate2D(Schema):
    """A 2D coordinate point with x and y values."""

    x: float
    y: float


# A polygon is a list of at least 3 coordinate points
PolygonShape = t.Annotated[list[Coordinate2D], Field(min_length=3)]


def point_in_polygon(point: Coordinate2D, polygon: list[Coordinate2D]) -> bool:
    """Check if a point is inside a polygon using ray casting algorithm.

    Args:
        point: Coordinate2D with x and y values
        polygon: List of Coordinate2D points forming the polygon vertices

    Returns:
        True if point is inside the polygon, False otherwise
    """
    x, y = point.x, point.y
    n = len(polygon)
    inside = False

    p1x, p1y = polygon[0].x, polygon[0].y
    for i in range(1, n + 1):
        p2x, p2y = polygon[i % n].x, polygon[i % n].y
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
        p1x, p1y = p2x, p2y

    return inside


class VenueSeatSchema(ModelSchema):
    """Schema for venue seat response.

    The `available` field defaults to True and can be overridden when returning
    seat availability for ticket purchase (e.g., via annotate or manual setting).
    """

    position: Coordinate2D | None = None
    available: bool = True  # For availability endpoints: False if taken by PENDING/ACTIVE ticket

    class Meta:
        model = VenueSeat
        fields = [
            "id",
            "label",
            "row",
            "number",
            "position",
            "is_accessible",
            "is_obstructed_view",
            "is_active",
        ]


class MinimalSeatSchema(ModelSchema):
    """Minimal seat schema for ticket responses."""

    class Meta:
        model = VenueSeat
        fields = ["id", "label", "row", "number", "is_accessible", "is_obstructed_view"]


class VenueSectorSchema(ModelSchema):
    """Schema for venue sector response (without seats)."""

    shape: list[Coordinate2D] | None = None
    metadata: dict[str, t.Any] | None = None

    class Meta:
        model = VenueSector
        fields = [
            "id",
            "name",
            "code",
            "shape",
            "capacity",
            "display_order",
            "metadata",
        ]


class VenueSectorWithSeatsSchema(VenueSectorSchema):
    """Schema for venue sector with nested seats."""

    seats: list[VenueSeatSchema] = Field(default_factory=list)


class VenueSchema(ModelSchema, CityRetrieveMixin):
    """Schema for venue response."""

    class Meta:
        model = Venue
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "capacity",
            "address",
            "location_maps_url",
            "location_maps_embed",
        ]


class VenueDetailSchema(VenueSchema):
    """Schema for venue detail response with sectors (no seats)."""

    sectors: list[VenueSectorSchema] = Field(default_factory=list)


class VenueWithSeatsSchema(VenueSchema):
    """Schema for venue with all sectors and seats."""

    sectors: list[VenueSectorWithSeatsSchema] = Field(default_factory=list)


# ---- Venue Availability Schemas (for ticket purchase flow) ----


class SectorAvailabilitySchema(Schema):
    """Sector with seat availability info.

    Extends VenueSectorSchema fields with availability counts.
    Uses VenueSeatSchema with `available` field for seat status.
    """

    id: UUID
    name: str
    code: str | None = None
    shape: list[Coordinate2D] | None = None
    capacity: int | None = None
    display_order: int = 0
    metadata: dict[str, t.Any] | None = None  # For frontend rendering (e.g., aisle positions)
    seats: list[VenueSeatSchema] = Field(default_factory=list)
    available_count: int = 0  # Number of available seats
    total_count: int = 0  # Total active seats


class VenueAvailabilitySchema(Schema):
    """Venue layout with seat availability for ticket purchase."""

    id: UUID
    name: str
    sectors: list[SectorAvailabilitySchema] = Field(default_factory=list)
    total_available: int = 0  # Total available seats across all sectors
    total_capacity: int = 0  # Total seats across all sectors


class VenueCreateSchema(CityEditMixin):
    """Schema for creating a venue."""

    name: OneToOneFiftyString
    description: StrippedString | None = None
    capacity: int | None = Field(None, ge=0)


class VenueUpdateSchema(CityEditMixin):
    """Schema for updating a venue."""

    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    capacity: int | None = Field(None, ge=0)


class VenueSeatInputSchema(Schema):
    """Schema for creating/updating a seat within a sector."""

    label: t.Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)]
    row: t.Annotated[str, StringConstraints(max_length=20, strip_whitespace=True)] | None = None
    number: int | None = Field(None, ge=0)
    position: Coordinate2D | None = Field(
        None,
        description="Seat position {x, y}. Must be within sector shape if shape is defined.",
    )
    is_accessible: bool = False
    is_obstructed_view: bool = False
    is_active: bool = True


class VenueSectorCreateSchema(Schema):
    """Schema for creating a sector with optional nested seats."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    code: t.Annotated[str, StringConstraints(max_length=30, strip_whitespace=True)] | None = None
    shape: PolygonShape | None = Field(
        None,
        description="Polygon vertices [{x, y}, ...] for FE rendering. Minimum 3 points.",
    )
    capacity: int | None = Field(None, ge=0)
    display_order: int = Field(0, ge=0)
    metadata: dict[str, t.Any] | None = Field(
        None,
        description="Arbitrary JSON metadata for frontend rendering (e.g., aisle positions).",
    )
    seats: list[VenueSeatInputSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_seat_positions(self) -> "VenueSectorCreateSchema":
        """Validate that seat positions are within the sector shape if both are defined."""
        if not self.shape or not self.seats:
            return self

        for seat in self.seats:
            if seat.position is not None:
                if not point_in_polygon(seat.position, self.shape):
                    raise ValueError(f"Seat '{seat.label}' position is outside the sector shape.")

        return self


class VenueSectorUpdateSchema(Schema):
    """Schema for updating a sector's metadata."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)] | None = None
    code: t.Annotated[str, StringConstraints(max_length=30, strip_whitespace=True)] | None = None
    shape: PolygonShape | None = Field(
        None,
        description="Polygon vertices [{x, y}, ...] for FE rendering. Minimum 3 points.",
    )
    capacity: int | None = Field(None, ge=0)
    display_order: int | None = Field(None, ge=0)
    metadata: dict[str, t.Any] | None = Field(
        None,
        description="Arbitrary JSON metadata for frontend rendering (e.g., aisle positions).",
    )


class VenueSeatBulkCreateSchema(Schema):
    """Schema for bulk creating seats in a sector."""

    seats: list[VenueSeatInputSchema] = Field(
        ...,
        min_length=1,
        description="List of seats to create in the sector.",
    )


class VenueSeatBulkDeleteSchema(Schema):
    """Schema for bulk deleting seats in a sector."""

    labels: list[t.Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)]] = Field(
        ...,
        min_length=1,
        description="List of seat labels to delete.",
    )


class VenueSeatUpdateSchema(Schema):
    """Schema for updating an individual seat."""

    row: t.Annotated[str, StringConstraints(max_length=20, strip_whitespace=True)] | None = None
    number: int | None = Field(None, ge=0)
    position: Coordinate2D | None = Field(
        None,
        description="Seat position {x, y}. Must be within sector shape if shape is defined.",
    )
    is_accessible: bool | None = None
    is_obstructed_view: bool | None = None
    is_active: bool | None = None


class VenueSeatBulkUpdateItemSchema(Schema):
    """Schema for a single seat update in bulk update operation.

    Identifies the seat by label and includes the fields to update.
    """

    label: t.Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)] = Field(
        ...,
        description="The label of the seat to update (identifier).",
    )
    row: t.Annotated[str, StringConstraints(max_length=20, strip_whitespace=True)] | None = None
    number: int | None = Field(None, ge=0)
    position: Coordinate2D | None = Field(
        None,
        description="Seat position {x, y}. Must be within sector shape if shape is defined.",
    )
    is_accessible: bool | None = None
    is_obstructed_view: bool | None = None
    is_active: bool | None = None


class VenueSeatBulkUpdateSchema(Schema):
    """Schema for bulk updating seats in a sector."""

    seats: list[VenueSeatBulkUpdateItemSchema] = Field(
        ...,
        min_length=1,
        description="List of seats to update with their new values.",
    )
