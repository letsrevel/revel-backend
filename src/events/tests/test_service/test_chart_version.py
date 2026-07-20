"""Every venue write that changes what the chart renders must move the chart version (#752).

The version is what a buyer's open seat map polls. Before this, it was derived from
``max(venue, sector, seat .updated_at)`` — which no ``DELETE`` can move, which never included
price categories at all, and which ``save(update_fields=...)``, ``bulk_update()`` and
``queryset.update()`` all silently bypass because Django drops unlisted ``auto_now`` fields.
The single-seat ``PATCH`` could therefore repaint a seat — a money-affecting change, guarded on
the bulk endpoint by #747's report — with nothing moving at all.

One test per writer. Each asserts the version *strictly increased*, read back through
``resolve_chart_version`` (what the poller sees), not off the in-memory instance.
"""

import datetime
import uuid

import pytest

from events import schema
from events.models import Organization, PriceCategory, Venue, VenueSeat, VenueSector
from events.service import venue_service
from events.service.seating import availability, chart

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
def standard(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")


def version(venue_id: uuid.UUID) -> datetime.datetime:
    """The version as the buyer's poller sees it."""
    resolved = availability.resolve_chart_version(venue_id)
    assert resolved is not None
    return resolved


class TestVersionMovesOnEveryWriter:
    def test_update_venue(self, venue: Venue) -> None:
        before = version(venue.id)
        venue_service.update_venue(venue, schema.VenueUpdateSchema(name="Renamed Hall"))  # type: ignore[call-arg]
        assert version(venue.id) > before

    def test_create_price_category(self, venue: Venue) -> None:
        before = version(venue.id)
        venue_service.create_price_category(
            venue,
            schema.PriceCategoryCreateSchema(name="Premium", color="#aa0000"),  # type: ignore[call-arg]
        )
        assert version(venue.id) > before

    def test_update_price_category(self, venue: Venue, category: PriceCategory) -> None:
        """A rename recolours the chart's legend — and categories were never in the old max()."""
        before = version(venue.id)
        venue_service.update_price_category(category, schema.PriceCategoryUpdateSchema(name="Gold"))  # type: ignore[call-arg]
        assert version(venue.id) > before

    def test_delete_price_category(self, venue: Venue, category: PriceCategory) -> None:
        before = version(venue.id)
        venue_service.delete_price_category(category)
        assert version(venue.id) > before

    def test_create_sector(self, venue: Venue) -> None:
        before = version(venue.id)
        venue_service.create_sector(venue, schema.VenueSectorCreateSchema(name="Balcony"))  # type: ignore[call-arg]
        assert version(venue.id) > before

    def test_update_sector(self, venue: Venue, sector: VenueSector) -> None:
        before = version(venue.id)
        venue_service.update_sector(sector, schema.VenueSectorUpdateSchema(name="Orchestra"))  # type: ignore[call-arg]
        assert version(venue.id) > before

    def test_delete_sector(self, venue: Venue, sector: VenueSector) -> None:
        before = version(venue.id)
        venue_service.delete_sector(sector)
        assert version(venue.id) > before

    def test_bulk_create_seats(self, venue: Venue, sector: VenueSector) -> None:
        before = version(venue.id)
        venue_service.bulk_create_seats(sector, [schema.VenueSeatInputSchema(label="A1")])  # type: ignore[call-arg]
        assert version(venue.id) > before

    def test_update_seat(self, venue: Venue, sector: VenueSector) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        before = version(venue.id)
        venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(is_active=False))  # type: ignore[call-arg]
        assert version(venue.id) > before

    def test_update_seat_repainting_a_price_category(
        self, venue: Venue, sector: VenueSector, category: PriceCategory, standard: PriceCategory
    ) -> None:
        """The headline: a single-seat repaint moves money, so the poller has to see it.

        This PATCH bypasses the bulk paint endpoint's #747 advisory entirely, and used to
        leave the version frozen: the buyer kept the old colour *and the old price* on screen
        for as long as the tab stayed open, then got charged the new one at checkout.
        """
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        before = version(venue.id)

        venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(price_category_id=standard.id))  # type: ignore[call-arg]

        seat.refresh_from_db()
        assert seat.default_price_category_id == standard.id
        assert version(venue.id) > before

    def test_delete_seat(self, venue: Venue, sector: VenueSector) -> None:
        """A DELETE leaves no row to stamp; a derived max() could never have moved here."""
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        before = version(venue.id)
        venue_service.delete_seat(seat)
        assert version(venue.id) > before

    def test_bulk_delete_seats(self, venue: Venue, sector: VenueSector) -> None:
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")
        before = version(venue.id)
        assert venue_service.bulk_delete_seats(sector, ["A1", "A2"]) == 2
        assert version(venue.id) > before

    def test_bulk_update_seats(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        VenueSeat.objects.create(sector=sector, label="A1")
        before = version(venue.id)
        venue_service.bulk_update_seats(
            sector,
            [schema.VenueSeatBulkUpdateItemSchema(label="A1", price_category_id=category.id)],  # type: ignore[call-arg]
        )
        assert version(venue.id) > before

    def test_paint_seats(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        before = version(venue.id)
        venue_service.paint_seats(venue, schema.VenueSeatPaintSchema(seat_ids=[seat.id], price_category_id=category.id))
        assert version(venue.id) > before

    def test_paint_preview_does_not_move_it(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        """A dry run writes nothing, so it must not invalidate anybody's open chart."""
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        before = version(venue.id)
        venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seat.id], price_category_id=category.id), preview=True
        )
        assert version(venue.id) == before

    def test_derive_sector_seat_ranks(self, venue: Venue, sector: VenueSector) -> None:
        """Re-ranking rewrites chart-visible fields on seats no caller named.

        ``bulk_update`` bypasses ``auto_now``, and this runs sector-wide, so it owns its own
        bump rather than relying on whichever caller happened to also write something.
        """
        VenueSeat.objects.bulk_create(
            [VenueSeat(sector=sector, label="A1", row_label="A", number=1, row_order=7, adjacency_index=7)]
        )
        before = version(venue.id)

        venue_service.derive_sector_seat_ranks(sector)

        assert VenueSeat.objects.get(label="A1").adjacency_index == 0
        assert version(venue.id) > before

    def test_derive_is_a_no_op_when_ranks_already_agree(self, venue: Venue, sector: VenueSector) -> None:
        VenueSeat.objects.create(sector=sector, label="A1", row_label="A", number=1)
        venue_service.derive_sector_seat_ranks(sector)
        before = version(venue.id)

        venue_service.derive_sector_seat_ranks(sector)

        assert version(venue.id) == before


class TestTheTwoDefinitionsAgree:
    """``build_chart`` and ``resolve_chart_version`` are one column, not two implementations."""

    def test_equal_after_every_kind_of_write(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        def assert_agree() -> None:
            fresh = Venue.objects.get(pk=venue.id)
            assert chart.build_chart(fresh).updated_at == version(venue.id)

        assert_agree()
        venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(is_active=False))  # type: ignore[call-arg]
        assert_agree()
        venue_service.update_sector(sector, schema.VenueSectorUpdateSchema(name="Orchestra"))  # type: ignore[call-arg]
        assert_agree()
        venue_service.update_price_category(category, schema.PriceCategoryUpdateSchema(name="Gold"))  # type: ignore[call-arg]
        assert_agree()
        venue_service.delete_seat(VenueSeat.objects.get(pk=seat.id))
        assert_agree()
        venue_service.delete_price_category(PriceCategory.objects.get(pk=category.id))
        assert_agree()
