"""Coordinate2D tolerant coercion: legacy ``[x, y]`` pairs heal to canonical ``{x, y}``."""

import pydantic
import pytest

from events import schema
from events.models import Organization, Venue, VenueSector


def test_canonical_dict_accepted() -> None:
    coord = schema.Coordinate2D.model_validate({"x": 1, "y": 2.5})
    assert (coord.x, coord.y) == (1.0, 2.5)


def test_legacy_list_pair_coerced() -> None:
    coord = schema.Coordinate2D.model_validate([1, 2.5])
    assert (coord.x, coord.y) == (1.0, 2.5)


def test_legacy_tuple_pair_coerced() -> None:
    coord = schema.Coordinate2D.model_validate((3, 4))
    assert (coord.x, coord.y) == (3.0, 4.0)


@pytest.mark.parametrize("bad", [[1], [1, 2, 3], [], "x", ["a", "b"], {"x": 1}])
def test_malformed_coordinate_rejected(bad: object) -> None:
    with pytest.raises(pydantic.ValidationError):
        schema.Coordinate2D.model_validate(bad)


@pytest.mark.django_db
def test_sector_schema_serializes_legacy_shape(organization: Organization) -> None:
    """Tier→sector response nesting (checkout path) must not 500 on legacy-format DB rows."""
    venue = Venue.objects.create(organization=organization, name="Hall")
    sector = VenueSector.objects.create(venue=venue, name="Stalls", shape=[[0, 0], [4, 0], [4, 2], [0, 2]])
    dumped = schema.VenueSectorSchema.from_orm(sector).model_dump()
    assert dumped["shape"] == [
        {"x": 0.0, "y": 0.0},
        {"x": 4.0, "y": 0.0},
        {"x": 4.0, "y": 2.0},
        {"x": 0.0, "y": 2.0},
    ]
