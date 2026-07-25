"""Shared size/depth bounds on organizer-written venue/sector ``metadata`` (#761).

The blob is public (served on the anonymous seating chart), so the write path caps
it at ``METADATA_MAX_BYTES`` of compact JSON and ``METADATA_MAX_DEPTH`` nested
containers on all four write surfaces: venue create/update, sector create/update.
"""

import json
import typing as t

import pytest
from pydantic import ValidationError

from events.schema.venue import (
    METADATA_MAX_BYTES,
    METADATA_MAX_DEPTH,
    VenueCreateSchema,
    VenueSectorCreateSchema,
    VenueSectorUpdateSchema,
    VenueUpdateSchema,
)

MetadataFactory = t.Callable[[dict[str, t.Any] | None], t.Any]

# One factory per metadata write surface, so every case below runs against all four.
FACTORIES: dict[str, MetadataFactory] = {
    "venue_create": lambda md: VenueCreateSchema(name="Hall", metadata=md),  # type: ignore[call-arg]
    "venue_update": lambda md: VenueUpdateSchema(metadata=md),  # type: ignore[call-arg]
    "sector_create": lambda md: VenueSectorCreateSchema(name="Floor", metadata=md),  # type: ignore[call-arg]
    "sector_update": lambda md: VenueSectorUpdateSchema(metadata=md),  # type: ignore[call-arg]
}


def _nested(depth: int) -> dict[str, t.Any]:
    """A metadata dict whose deepest path crosses exactly ``depth`` containers."""
    value: dict[str, t.Any] = {"leaf": 1}
    for _ in range(depth - 1):
        value = {"nested": value}
    return value


@pytest.mark.parametrize("factory", FACTORIES.values(), ids=FACTORIES.keys())
def test_oversized_metadata_rejected(factory: MetadataFactory) -> None:
    oversized = {"junk": "x" * METADATA_MAX_BYTES}
    with pytest.raises(ValidationError, match="bytes"):
        factory(oversized)


@pytest.mark.parametrize("factory", FACTORIES.values(), ids=FACTORIES.keys())
def test_metadata_at_exact_byte_limit_accepted(factory: MetadataFactory) -> None:
    # {"k":"xxx...x"} — compact JSON overhead is 8 bytes, so pad to exactly the cap.
    boundary = {"k": "x" * (METADATA_MAX_BYTES - 8)}
    assert len(json.dumps(boundary, separators=(",", ":"), ensure_ascii=False).encode()) == METADATA_MAX_BYTES
    assert factory(boundary).metadata == boundary


@pytest.mark.parametrize("factory", FACTORIES.values(), ids=FACTORIES.keys())
def test_one_byte_over_limit_rejected(factory: MetadataFactory) -> None:
    over = {"k": "x" * (METADATA_MAX_BYTES - 7)}
    assert len(json.dumps(over, separators=(",", ":"), ensure_ascii=False).encode()) == METADATA_MAX_BYTES + 1
    with pytest.raises(ValidationError, match="bytes"):
        factory(over)


@pytest.mark.parametrize("factory", FACTORIES.values(), ids=FACTORIES.keys())
def test_over_deep_metadata_rejected(factory: MetadataFactory) -> None:
    with pytest.raises(ValidationError, match="nest deeper"):
        factory(_nested(METADATA_MAX_DEPTH + 1))


@pytest.mark.parametrize("factory", FACTORIES.values(), ids=FACTORIES.keys())
def test_metadata_at_exact_depth_limit_accepted(factory: MetadataFactory) -> None:
    boundary = _nested(METADATA_MAX_DEPTH)
    assert factory(boundary).metadata == boundary


@pytest.mark.parametrize("factory", FACTORIES.values(), ids=FACTORIES.keys())
def test_null_metadata_accepted(factory: MetadataFactory) -> None:
    assert factory(None).metadata is None


@pytest.mark.parametrize("factory", [FACTORIES["venue_create"], FACTORIES["venue_update"]], ids=["create", "update"])
def test_real_world_venue_metadata_accepted(factory: MetadataFactory) -> None:
    """The stage config the designer actually writes (venue-overview.ts contract)."""
    stage = {
        "stage": {
            "position": {"x": 5.0, "y": -2.0},
            "shape": [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}, {"x": 10.0, "y": 2.0}],
            "label": "Stage",
        }
    }
    assert factory(stage).metadata == stage


@pytest.mark.parametrize("factory", [FACTORIES["sector_create"], FACTORIES["sector_update"]], ids=["create", "update"])
def test_real_world_sector_metadata_accepted(factory: MetadataFactory) -> None:
    """The transform/aisles config the grid editor actually writes (seat-map-layout.ts contract)."""
    md = {
        "transform": {"x": 0.0, "y": 120.0, "rotation": 90.0},
        "aisles": {"verticalAisles": [4, 8], "horizontalAisles": [2], "invertRowOrder": False},
    }
    assert factory(md).metadata == md
