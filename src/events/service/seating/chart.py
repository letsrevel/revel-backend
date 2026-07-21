"""Render-ready chart JSON straight from live tables (spec §1 — no versioning in v1).

Self-sufficient prefetch (sectors, seats, price categories) keeps this at a constant
query count regardless of how the caller fetched the venue.

This module also owns *the chart version* — the single ``Venue.chart_version`` column that
:func:`build_chart` emits, :func:`events.service.seating.availability.resolve_chart_version`
polls, and :func:`bump_chart_version` is the only writer of.
"""

import datetime
import typing as t
import uuid

from django.utils import timezone

from events.models import Venue, VenueSector
from events.schema.seating import (
    CHART_SECTOR_METADATA_KEYS,
    CHART_VENUE_METADATA_KEYS,
    ChartSeatSchema,
    ChartSectorSchema,
    VenueChartSchema,
)
from events.schema.venue import PriceCategorySchema


def _project_metadata(metadata: dict[str, t.Any] | None, allowed: frozenset[str]) -> dict[str, t.Any] | None:
    """Reduce organizer-written metadata to the chart's public whitelist (#761).

    ``None`` stays ``None`` (the FE's one emptiness check); an object keeps only its
    whitelisted keys, verbatim — possibly ``{}``. The whitelists live next to the chart
    schemas in :mod:`events.schema.seating`.
    """
    if metadata is None:
        return None
    return {k: v for k, v in metadata.items() if k in allowed}


def bump_chart_version(venue_id: uuid.UUID) -> datetime.datetime:
    """Move a venue's chart version so open buyer charts refetch.

    Call this from **every** venue write that changes what the chart renders — including
    deletes, which is precisely what the old derived ``max(updated_at)`` could not express.
    One targeted UPDATE on one row; writes are not a hot path.

    Args:
        venue_id: The venue whose chart changed.

    Returns:
        The new version, so a caller holding the ``Venue`` instance can keep it in sync
        (``venue.chart_version = bump_chart_version(venue.id)``) instead of refetching.
    """
    now = timezone.now()
    Venue.objects.filter(pk=venue_id).update(chart_version=now)
    return now


def build_chart(venue: Venue) -> VenueChartSchema:
    """Serialize a venue's full seating layout into a single render-ready chart payload.

    ``updated_at`` is read straight off the passed instance's ``chart_version``, so the
    caller must pass a freshly-read venue — the same requirement the prefetches already imply.
    """
    sectors = venue.sectors.prefetch_related("seats").all()
    categories = list(venue.price_categories.all())
    sector_schemas: list[ChartSectorSchema] = []
    for sector in sectors:
        seat_schemas: list[ChartSeatSchema] = []
        for seat in sector.seats.all():
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
                kind=VenueSector.Kind(sector.kind),
                shape=sector.shape,
                capacity=sector.capacity,
                display_order=sector.display_order,
                metadata=_project_metadata(sector.metadata, CHART_SECTOR_METADATA_KEYS),
                seats=seat_schemas,
            )
        )
    return VenueChartSchema(
        venue_id=venue.id,
        venue_name=venue.name,
        updated_at=venue.chart_version,
        metadata=_project_metadata(venue.metadata, CHART_VENUE_METADATA_KEYS),
        price_categories=[PriceCategorySchema.from_orm(c) for c in categories],
        sectors=sector_schemas,
    )
