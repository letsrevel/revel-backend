"""Tests for Phase-1 seating models (PriceCategory, sector kind, seat columns)."""

import pytest
from django.core.exceptions import ValidationError

from events.models import Organization, PriceCategory, Venue, VenueSeat, VenueSector

pytestmark = pytest.mark.django_db


@pytest.fixture
def venue(organization: Organization) -> Venue:
    return Venue.objects.create(organization=organization, name="Main Hall")


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Stalls")


def test_price_category_creation_and_uniqueness(venue: Venue) -> None:
    cat = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")
    assert cat.display_order == 0
    # TimeStampedModel.save() runs full_clean(), so the (venue, name) uniqueness
    # constraint surfaces as a ValidationError before the DB-level IntegrityError.
    with pytest.raises(ValidationError):
        PriceCategory.objects.create(venue=venue, name="Premium", color="#bb0000")


def test_sector_kind_defaults_to_seated(sector: VenueSector) -> None:
    assert sector.kind == VenueSector.Kind.SEATED


def test_seat_row_and_adjacency_columns(sector: VenueSector, venue: Venue) -> None:
    cat = PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")
    seat = VenueSeat.objects.create(
        sector=sector,
        label="A1",
        row_label="A",
        row_order=0,
        number=1,
        adjacency_index=0,
        default_price_category=cat,
    )
    assert seat.row_label == "A"
    assert seat.adjacency_index == 0
    assert seat.default_price_category == cat


def test_seat_label_composition_unchanged(sector: VenueSector) -> None:
    """Spec §6.4: composed labels must not change through the migration.

    `label` is an independent column; renaming row→row_label must not affect it.
    """
    seat = VenueSeat.objects.create(sector=sector, label="C-12", row_label="C", number=12)
    assert seat.label == "C-12"
    assert str(seat) == f"{sector.name} / C-12"
