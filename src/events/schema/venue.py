"""Venue-related schemas."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from ninja.schema import DjangoGetter
from pydantic import Field, StringConstraints, model_validator

from common.schema import OneToOneFiftyString, StrippedString
from events.models import Event, PriceCategory, Venue, VenueSeat, VenueSector

from .mixins import CityEditMixin, CityRetrieveMixin


class Coordinate2D(Schema):
    """A 2D coordinate point with x and y values.

    Canonical form is an ``{"x": .., "y": ..}`` mapping; legacy 2-element
    ``[x, y]`` pairs (older DB rows) are coerced on validation.
    """

    x: float
    y: float

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_pair(cls, value: t.Any) -> t.Any:
        """Coerce a legacy 2-element ``[x, y]`` sequence into an ``{x, y}`` mapping."""
        # Ninja's Schema wrap-validator hands nested values to us as DjangoGetter.
        raw = value._obj if isinstance(value, DjangoGetter) else value
        if isinstance(raw, (list, tuple)):
            if len(raw) != 2:
                raise ValueError("Coordinate must be an {'x': .., 'y': ..} mapping or a 2-element [x, y] pair.")
            return {"x": raw[0], "y": raw[1]}
        return value


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
        if y > min(p1y, p2y) and y <= max(p1y, p2y) and x <= max(p1x, p2x) and p1y != p2y:
            xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
            if p1x == p2x or x <= xinters:
                inside = not inside
        p1x, p1y = p2x, p2y

    return inside


class PriceCategorySchema(ModelSchema):
    """Venue price category (map color + name)."""

    class Meta:
        model = PriceCategory
        fields = ["id", "name", "color", "display_order"]


# Hex color for map rendering, e.g. ``#aa0000``.
HexColor = t.Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class PriceCategoryCreateSchema(Schema):
    """Schema for creating a venue price category."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    color: HexColor
    display_order: int = Field(0, ge=0)


class PriceCategoryUpdateSchema(Schema):
    """Schema for updating a venue price category."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)] | None = None
    color: HexColor | None = None
    display_order: int | None = Field(None, ge=0)


class VenueSeatSchema(ModelSchema):
    """Schema for venue seat response.

    The `available` field defaults to True and can be overridden when returning
    seat availability for ticket purchase (e.g., via annotate or manual setting).
    """

    position: Coordinate2D | None = None
    available: bool = True  # For availability endpoints: False if taken by any non-cancelled ticket
    row_label: str | None = None
    # Transitional alias so the deployed FE (reads `row`) keeps working until Phase 2 regen.
    row: str | None = None
    # Paint round-trip: expose the seat's category so the admin grid editor can re-hydrate
    # existing paint on reload. `price_category_id` mirrors ChartSeatSchema; `price_category`
    # carries the color/name for rendering. The model field is `default_price_category`.
    price_category_id: UUID | None = None
    price_category: PriceCategorySchema | None = None

    class Meta:
        model = VenueSeat
        fields = [
            "id",
            "label",
            "number",
            "position",
            "is_accessible",
            "is_obstructed_view",
            "is_active",
        ]

    @staticmethod
    def resolve_row(obj: VenueSeat) -> str | None:
        """Transitional alias exposing `row_label` under the legacy `row` key."""
        return obj.row_label

    @staticmethod
    def resolve_price_category_id(obj: VenueSeat) -> UUID | None:
        """Expose the seat's `default_price_category` FK id under `price_category_id`."""
        return obj.default_price_category_id

    @staticmethod
    def resolve_price_category(obj: VenueSeat) -> PriceCategory | None:
        """Expose the resolved category object (color/name) for grid rendering."""
        return obj.default_price_category


class MinimalSeatSchema(ModelSchema):
    """Minimal seat schema for ticket responses."""

    row_label: str | None = None
    # Transitional alias so the deployed FE (reads `row`) keeps working until Phase 2 regen.
    row: str | None = None

    class Meta:
        model = VenueSeat
        fields = ["id", "label", "number", "is_accessible", "is_obstructed_view"]

    @staticmethod
    def resolve_row(obj: VenueSeat) -> str | None:
        """Transitional alias exposing `row_label` under the legacy `row` key."""
        return obj.row_label


class VenueSectorSchema(ModelSchema):
    """Schema for venue sector response (without seats)."""

    shape: list[Coordinate2D] | None = None
    metadata: dict[str, t.Any] | None = None
    # Exposed so the admin SectorModal can prefill the current kind on edit.
    kind: VenueSector.Kind = VenueSector.Kind.SEATED

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

    metadata: dict[str, t.Any] | None = None

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
            "metadata",
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
    metadata: dict[str, t.Any] | None = Field(
        default=None,
        description="Arbitrary JSON for venue-level layout config (e.g. stage position/shape).",
    )


class VenueUpdateSchema(CityEditMixin):
    """Schema for updating a venue."""

    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    capacity: int | None = Field(None, ge=0)
    metadata: dict[str, t.Any] | None = Field(
        default=None,
        description="Arbitrary JSON for venue-level layout config (e.g. stage position/shape).",
    )


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
    price_category_id: UUID | None = Field(
        default=None,
        description="Price category to paint the seat with. Must belong to the sector's venue. Null = unpainted.",
    )
    row_order: int | None = Field(
        default=None,
        ge=0,
        description="Explicit front-to-back row rank. Omit to have it derived from row labels.",
    )
    adjacency_index: int | None = Field(
        default=None,
        ge=0,
        description="Explicit left-to-right position in the row. Omit to have it derived from seat numbers.",
    )


class VenueSectorCreateSchema(Schema):
    """Schema for creating a sector with optional nested seats."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    code: t.Annotated[str, StringConstraints(max_length=30, strip_whitespace=True)] | None = None
    kind: VenueSector.Kind = VenueSector.Kind.SEATED
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
    kind: VenueSector.Kind | None = Field(
        default=None,
        description="Sector kind. May only change while the sector has zero seats.",
    )
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
    price_category_id: UUID | None = Field(
        default=None,
        description="Price category to paint the seat with. Must belong to the sector's venue. Null = unpaint.",
    )
    row_order: int | None = Field(
        default=None,
        ge=0,
        description="Explicit front-to-back row rank. Omit to have it derived from row labels.",
    )
    adjacency_index: int | None = Field(
        default=None,
        ge=0,
        description="Explicit left-to-right position in the row. Omit to have it derived from seat numbers.",
    )


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
    price_category_id: UUID | None = Field(
        default=None,
        description="Price category to paint the seat with. Must belong to the sector's venue. Null = unpaint.",
    )
    row_order: int | None = Field(
        default=None,
        ge=0,
        description="Explicit front-to-back row rank. Omit to have it derived from row labels.",
    )
    adjacency_index: int | None = Field(
        default=None,
        ge=0,
        description="Explicit left-to-right position in the row. Omit to have it derived from seat numbers.",
    )


class VenueSeatBulkUpdateSchema(Schema):
    """Schema for bulk updating seats in a sector."""

    seats: list[VenueSeatBulkUpdateItemSchema] = Field(
        ...,
        min_length=1,
        description="List of seats to update with their new values.",
    )


class VenueSeatPaintSchema(Schema):
    """Schema for bulk painting seats with a price category (or unpainting with null)."""

    seat_ids: list[UUID] = Field(
        ...,
        min_length=1,
        description="Seats to paint. All must belong to the venue.",
    )
    price_category_id: UUID | None = Field(
        ...,
        description="Price category to paint the seats with (must belong to the venue). Null = unpaint.",
    )


class TierPricingGapSchema(Schema):
    """A price category painted in the tier's sector that the tier does not price."""

    id: UUID
    name: str
    color: str


class TierUnsellableZoneSchema(Schema):
    """The converse of a pricing gap: a zone the tier prices that no live seat carries.

    Best-available only, and never the same condition as ``TierPricingGapSchema`` —
    a *painted-but-unpriced* category is that tier's deliberate scoping, while a
    *priced-but-unpainted* one is a zone the buyer can select and the picker can never
    fill, so every purchase of it 409s.
    """

    id: UUID
    name: str
    color: str


class SeatPriceChangeSchema(Schema):
    """One before→after price move a paint caused on a tier, and how many seats made it.

    A single paint writes one category, but the seats it overwrites can have come from
    several different ones, so a tier can report more than one move.

    Attributes:
        seat_count: Active seats in this paint that moved from ``from_price`` to ``to_price``.
        from_price: What each of those seats cost on this tier **before** the paint.
            ``None`` means the seat was in a category the tier does not price, so there was
            no honest number — see ``to_price`` for what that means per seating mode.
        to_price: What each now costs. ``None`` means the paint moved the seats into a
            category this tier has no price for, which reads differently by seat-assignment
            mode: on a **user_choice** tier the seat stays pickable but checkout refuses it
            (spec §4.3), while on a **best_available** tier a partial map is legal and the
            keys define the sellable zones — so the seat simply left the tier's pool and the
            picker will never offer it again. Either way the tier can no longer sell it.
    """

    seat_count: int
    from_price: Decimal | None = None
    to_price: Decimal | None = None


class PaintReportTierSchema(Schema):
    """Which tier, on which event, a seat-paint advisory entry is about.

    Attributes:
        event_status: So the frontend can rank a live on-sale above the draft the admin
            is currently configuring — both are worth reporting, but only one is urgent.
    """

    tier_id: UUID
    tier_name: str
    event_id: UUID
    event_name: str
    event_status: Event.EventStatus


class AffectedTierSchema(PaintReportTierSchema):
    """A live, category-priced tier this paint changed the economics of.

    A tier is reported when the paint moved the price of at least one of its seats
    (``price_changes``), or when it is left unable to sell some of its sector
    (``missing_categories``), or both.

    Attributes:
        price_changes: Empty when the paint moved nothing on this tier.
        missing_categories: The tier's **current** coverage gap, not this paint's delta —
            a category painted in its sector that it does not price. Non-empty means seats
            in those categories are refused at checkout until the tier prices them.
    """

    price_changes: list[SeatPriceChangeSchema] = Field(default_factory=list)
    missing_categories: list[TierPricingGapSchema] = Field(default_factory=list)


class UnsellableZoneTierSchema(PaintReportTierSchema):
    """A best-available tier left publishing a zone the painted sector cannot fill.

    Deliberately **not** folded into ``AffectedTierSchema``: the two populations are
    disjoint in both directions. A paint can reprice a tier that has no unsellable zone,
    and — because an unpaint whose category was priced at the flat price moves no money —
    it can strand a zone on a tier it did not reprice at all. Merging them would make an
    empty ``affected_tiers`` stop meaning "this paint moved no money".

    Attributes:
        zones: The tier's **current** unsellable zones, not this paint's delta — same tense
            as ``AffectedTierSchema.missing_categories``, its converse. Every zone listed
            409s for any buyer who selects it until the sector is repainted or the key is
            dropped from the map.
    """

    zones: list[TierUnsellableZoneSchema] = Field(default_factory=list)


class SeatPaintResultSchema(Schema):
    """Result of a bulk paint: what changed, and what it cost or left unsellable."""

    painted: int = Field(..., description="Number of seats updated.")
    affected_tiers: list[AffectedTierSchema] = Field(
        default_factory=list,
        description=(
            "User-choice, category-priced tiers on the painted sectors whose seat prices this "
            "paint changed, and/or whose price map no longer covers every category painted in "
            "their sector. Painting is venue-scoped, so this is the only place the blast radius "
            "across other events is visible. Advisory only — the paint itself always succeeds."
        ),
    )
    unsellable_zone_tiers: list[UnsellableZoneTierSchema] = Field(
        default_factory=list,
        description=(
            "Best-available tiers on the painted sectors that price a zone no live seat there "
            "carries — the converse of a pricing gap, and never deliberate: buyers can select "
            "the zone and the picker can never fill it, so every purchase 409s. Unpainting (or "
            "repainting away) the last seat of a zone is the usual cause, which is why it is "
            "reported here rather than only on the tier screen. Advisory only."
        ),
    )
