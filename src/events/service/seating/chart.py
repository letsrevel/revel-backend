"""Render-ready chart JSON straight from live tables (spec §1 — no versioning in v1).

Self-sufficient prefetch (sectors, seats, price categories) keeps this at a constant
query count regardless of how the caller fetched the venue.
"""

from events.models import Venue
from events.schema.seating import ChartSeatSchema, ChartSectorSchema, VenueChartSchema
from events.schema.venue import PriceCategorySchema


def build_chart(venue: Venue) -> VenueChartSchema:
    """Serialize a venue's full seating layout into a single render-ready chart payload."""
    sectors = venue.sectors.prefetch_related("seats").all()
    categories = list(venue.price_categories.all())
    updated_candidates = [venue.updated_at]
    sector_schemas: list[ChartSectorSchema] = []
    for sector in sectors:
        updated_candidates.append(sector.updated_at)
        seat_schemas: list[ChartSeatSchema] = []
        for seat in sector.seats.all():
            updated_candidates.append(seat.updated_at)
            seat_schemas.append(
                ChartSeatSchema(
                    id=seat.id,
                    label=seat.label,
                    row_label=seat.row_label,
                    row_order=seat.row_order,
                    number=seat.number,
                    adjacency_index=seat.adjacency_index,
                    position=seat.position,
                    is_accessible=seat.is_accessible,
                    is_obstructed_view=seat.is_obstructed_view,
                    is_active=seat.is_active,
                    price_category_id=seat.default_price_category_id,
                )
            )
        sector_schemas.append(
            ChartSectorSchema(
                id=sector.id,
                name=sector.name,
                code=sector.code,
                kind=sector.kind,
                shape=sector.shape,
                capacity=sector.capacity,
                display_order=sector.display_order,
                metadata=sector.metadata,
                seats=seat_schemas,
            )
        )
    return VenueChartSchema(
        venue_id=venue.id,
        venue_name=venue.name,
        updated_at=max(updated_candidates),
        price_categories=[PriceCategorySchema.from_orm(c) for c in categories],
        sectors=sector_schemas,
    )
