"""Tests for seat painting: price_category_id on seat write schemas + paint_seats."""

import pytest
from ninja.errors import HttpError

from events import schema
from events.models import Organization, PriceCategory, Venue, VenueSeat, VenueSector
from events.service import venue_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def venue(organization: Organization) -> Venue:
    return Venue.objects.create(organization=organization, name="Main Hall")


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Stalls")


@pytest.fixture
def category(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")


@pytest.fixture
def other_venue_category(organization: Organization) -> PriceCategory:
    other = Venue.objects.create(organization=organization, name="Other Hall")
    return PriceCategory.objects.create(venue=other, name="Foreign", color="#00aa00")


class TestSeatCategoryOnWrites:
    """price_category_id on seat create/update payloads maps to default_price_category."""

    def test_bulk_create_paints_seats(self, sector: VenueSector, category: PriceCategory) -> None:
        seats = venue_service.bulk_create_seats(
            sector,
            [schema.VenueSeatInputSchema(label="A1", price_category_id=category.id)],  # type: ignore[call-arg]
        )
        assert seats[0].default_price_category_id == category.id

    def test_bulk_create_rejects_foreign_category(
        self, sector: VenueSector, other_venue_category: PriceCategory
    ) -> None:
        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_create_seats(
                sector,
                [schema.VenueSeatInputSchema(label="A1", price_category_id=other_venue_category.id)],  # type: ignore[call-arg]
            )
        assert exc_info.value.status_code == 400
        assert not sector.seats.exists()

    def test_create_sector_with_painted_seats(self, venue: Venue, category: PriceCategory) -> None:
        payload = schema.VenueSectorCreateSchema(  # type: ignore[call-arg]
            name="Balcony",
            seats=[schema.VenueSeatInputSchema(label="B1", price_category_id=category.id)],  # type: ignore[call-arg]
        )
        sector = venue_service.create_sector(venue, payload)
        assert sector.seats.get().default_price_category_id == category.id

    def test_create_sector_rejects_foreign_category(self, venue: Venue, other_venue_category: PriceCategory) -> None:
        payload = schema.VenueSectorCreateSchema(  # type: ignore[call-arg]
            name="Balcony",
            seats=[schema.VenueSeatInputSchema(label="B1", price_category_id=other_venue_category.id)],  # type: ignore[call-arg]
        )
        with pytest.raises(HttpError) as exc_info:
            venue_service.create_sector(venue, payload)
        assert exc_info.value.status_code == 400

    def test_update_seat_paints_and_unpaints(self, sector: VenueSector, category: PriceCategory) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(price_category_id=category.id))  # type: ignore[call-arg]
        seat.refresh_from_db()
        assert seat.default_price_category_id == category.id

        venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(price_category_id=None))  # type: ignore[call-arg]
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_update_seat_rejects_foreign_category(
        self, sector: VenueSector, other_venue_category: PriceCategory
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        with pytest.raises(HttpError) as exc_info:
            venue_service.update_seat(
                seat,
                schema.VenueSeatUpdateSchema(price_category_id=other_venue_category.id),  # type: ignore[call-arg]
            )
        assert exc_info.value.status_code == 400

    def test_bulk_update_paints_and_unpaints(self, sector: VenueSector, category: PriceCategory) -> None:
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=category)

        updated = venue_service.bulk_update_seats(
            sector,
            [
                schema.VenueSeatBulkUpdateItemSchema(label="A1", price_category_id=category.id),  # type: ignore[call-arg]
                schema.VenueSeatBulkUpdateItemSchema(label="A2", price_category_id=None),  # type: ignore[call-arg]
            ],
        )
        by_label = {s.label: s for s in updated}
        assert by_label["A1"].default_price_category_id == category.id
        assert by_label["A2"].default_price_category_id is None

    def test_bulk_update_rejects_foreign_category(
        self, sector: VenueSector, other_venue_category: PriceCategory
    ) -> None:
        VenueSeat.objects.create(sector=sector, label="A1")
        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_update_seats(
                sector,
                [schema.VenueSeatBulkUpdateItemSchema(label="A1", price_category_id=other_venue_category.id)],  # type: ignore[call-arg]
            )
        assert exc_info.value.status_code == 400


class TestPaintSeats:
    """Tests for the dedicated bulk paint service."""

    def test_paint_seats(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        seats = [VenueSeat.objects.create(sector=sector, label=f"A{i}") for i in range(1, 4)]

        painted = venue_service.paint_seats(
            venue,
            schema.VenueSeatPaintSchema(seat_ids=[s.id for s in seats[:2]], price_category_id=category.id),
        )

        assert painted == 2
        assert VenueSeat.objects.filter(default_price_category=category).count() == 2
        seats[2].refresh_from_db()
        assert seats[2].default_price_category_id is None

    def test_paint_seats_across_sectors(self, venue: Venue, category: PriceCategory) -> None:
        sector_a = VenueSector.objects.create(venue=venue, name="Left")
        sector_b = VenueSector.objects.create(venue=venue, name="Right")
        seat_a = VenueSeat.objects.create(sector=sector_a, label="L1")
        seat_b = VenueSeat.objects.create(sector=sector_b, label="R1")

        painted = venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seat_a.id, seat_b.id], price_category_id=category.id)
        )
        assert painted == 2

    def test_unpaint_with_null_category(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)

        painted = venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seat.id], price_category_id=None)
        )

        assert painted == 1
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_paint_rejects_foreign_category(
        self, venue: Venue, sector: VenueSector, other_venue_category: PriceCategory
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        with pytest.raises(HttpError) as exc_info:
            venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=[seat.id], price_category_id=other_venue_category.id)
            )
        assert exc_info.value.status_code == 400
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_paint_rejects_foreign_seats(
        self, venue: Venue, organization: Organization, category: PriceCategory
    ) -> None:
        other = Venue.objects.create(organization=organization, name="Other Hall")
        other_sector = VenueSector.objects.create(venue=other, name="Foreign")
        foreign_seat = VenueSeat.objects.create(sector=other_sector, label="X1")

        with pytest.raises(HttpError) as exc_info:
            venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=[foreign_seat.id], price_category_id=category.id)
            )
        assert exc_info.value.status_code == 404
        foreign_seat.refresh_from_db()
        assert foreign_seat.default_price_category_id is None
