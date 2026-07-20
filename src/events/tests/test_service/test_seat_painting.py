"""Tests for seat painting: price_category_id on seat write schemas + paint_seats."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from events import schema
from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector
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

        result = venue_service.paint_seats(
            venue,
            schema.VenueSeatPaintSchema(seat_ids=[s.id for s in seats[:2]], price_category_id=category.id),
        )

        assert result.painted == 2
        assert result.under_covered_tiers == []
        assert VenueSeat.objects.filter(default_price_category=category).count() == 2
        seats[2].refresh_from_db()
        assert seats[2].default_price_category_id is None

    def test_paint_seats_across_sectors(self, venue: Venue, category: PriceCategory) -> None:
        sector_a = VenueSector.objects.create(venue=venue, name="Left")
        sector_b = VenueSector.objects.create(venue=venue, name="Right")
        seat_a = VenueSeat.objects.create(sector=sector_a, label="L1")
        seat_b = VenueSeat.objects.create(sector=sector_b, label="R1")

        result = venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seat_a.id, seat_b.id], price_category_id=category.id)
        )
        assert result.painted == 2

    def test_unpaint_with_null_category(self, venue: Venue, sector: VenueSector, category: PriceCategory) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)

        result = venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seat.id], price_category_id=None)
        )

        assert result.painted == 1
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


FLAT = Decimal("50.00")


def _price_map(*categories: PriceCategory) -> dict[str, str]:
    return {str(c.id): "80.00" for c in categories}


def _make_tier(
    event: Event,
    venue: Venue,
    sector: VenueSector,
    *categories: PriceCategory,
    name: str = "Stalls",
    mode: str = TicketTier.SeatAssignmentMode.USER_CHOICE,
) -> TicketTier:
    """A tier on ``sector``, category-priced for ``categories`` (none = flat)."""
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=FLAT,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        venue=venue,
        sector=sector,
        seat_assignment_mode=mode,
        category_prices=_price_map(*categories),
    )


@pytest.fixture
def seated_venue_event(event: Event, venue: Venue) -> Event:
    event.venue = venue
    event.save(update_fields=["venue"])
    return event


@pytest.fixture
def standard(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa", display_order=1)


@pytest.fixture
def balcony(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00", display_order=2)


def _paint(venue: Venue, seats: list[VenueSeat], category: PriceCategory | None) -> schema.SeatPaintResultSchema:
    return venue_service.paint_seats(
        venue,
        schema.VenueSeatPaintSchema(
            seat_ids=[s.id for s in seats],
            price_category_id=category.id if category else None,
        ),
    )


class TestPaintUnderCoverageReport:
    """Paint always succeeds; the tiers it leaves unable to sell come back in the response.

    The report is advisory and deliberately quiet: it must stay empty in the common case
    (no category-priced tier anywhere near these sectors), or the frontend warning it feeds
    becomes noise the admin learns to dismiss.
    """

    def test_no_priced_tier_reports_nothing(
        self, venue: Venue, sector: VenueSector, standard: PriceCategory, seated_venue_event: Event
    ) -> None:
        """The common case: a flat tier on the sector cannot be under-covered."""
        _make_tier(seated_venue_event, venue, sector, name="General")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        assert _paint(venue, [seat], standard).under_covered_tiers == []

    def test_tier_made_under_covered_is_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """Painting a category the tier does not price names the tier, its event, and the gap."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        tier = _make_tier(seated_venue_event, venue, sector, standard)

        result = _paint(venue, [seat], balcony)

        assert result.painted == 1
        assert len(result.under_covered_tiers) == 1
        reported = result.under_covered_tiers[0]
        assert reported.tier_id == tier.id
        assert reported.tier_name == "Stalls"
        assert reported.event_id == seated_venue_event.id
        assert reported.event_name == seated_venue_event.name
        assert [(c.id, c.name, c.color) for c in reported.missing_categories] == [(balcony.id, "Balcony", "#00aa00")]

    def test_tier_pricing_everything_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        _make_tier(seated_venue_event, venue, sector, standard, balcony)

        assert _paint(venue, [seat], balcony).under_covered_tiers == []

    def test_best_available_tier_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """Only user-choice tiers read the category map; the rest cannot be under-covered."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        TicketTier.objects.create(
            event=seated_venue_event,
            name="Best",
            price=FLAT,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            price_category=standard,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )

        assert _paint(venue, [seat], balcony).under_covered_tiers == []

    def test_tier_on_another_sector_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """A tier selling a sector the paint never touched is unaffected by it."""
        other_sector = VenueSector.objects.create(venue=venue, name="Balcony Sector")
        other_seat = VenueSeat.objects.create(sector=other_sector, label="B1", default_price_category=standard)
        _make_tier(seated_venue_event, venue, other_sector, standard, name="Balcony Tier")
        painted_seat = VenueSeat.objects.create(sector=sector, label="A1")

        result = _paint(venue, [painted_seat], balcony)

        assert result.under_covered_tiers == []
        other_seat.refresh_from_db()
        assert other_seat.default_price_category_id == standard.id

    def test_past_event_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """Nobody can sell a finished event's seats, so its gaps are noise."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        _make_tier(seated_venue_event, venue, sector, standard)
        now = timezone.now()
        Event.objects.filter(pk=seated_venue_event.pk).update(
            start=now - timedelta(days=3), end=now - timedelta(days=2)
        )

        assert _paint(venue, [seat], balcony).under_covered_tiers == []

    def test_cancelled_event_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        _make_tier(seated_venue_event, venue, sector, standard)
        Event.objects.filter(pk=seated_venue_event.pk).update(status=Event.EventStatus.CANCELLED)

        assert _paint(venue, [seat], balcony).under_covered_tiers == []

    def test_unpaint_cannot_create_a_gap_and_closes_the_one_it_empties(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """Unpainting removes a category from the sector; it can only ever shrink the gap."""
        priced_seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        drifted = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)
        _make_tier(seated_venue_event, venue, sector, standard)
        assert len(_paint(venue, [drifted], balcony).under_covered_tiers) == 1

        result = _paint(venue, [drifted], None)

        assert result.painted == 1
        # The seat now falls back to the tier's flat price — the one legitimate fallback.
        assert result.under_covered_tiers == []
        priced_seat.refresh_from_db()
        assert priced_seat.default_price_category_id == standard.id

    def test_report_is_the_current_gap_not_the_delta(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """A gap this paint did not open is still a gap: the seats are still unsellable."""
        stuck = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        other = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)
        _make_tier(seated_venue_event, venue, sector, standard)
        _paint(venue, [stuck], balcony)

        # Repainting an unrelated seat into an already-priced category changes nothing,
        # but the balcony seat is still refused at checkout — say so.
        result = _paint(venue, [other], standard)

        assert [c.name for c in result.under_covered_tiers[0].missing_categories] == ["Balcony"]

    def test_query_count_does_not_scale_with_seats_painted(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Painting hundreds of seats must cost the same as painting two.

        The report adds three bounded queries (affected tiers, what is painted on the
        sectors, the missing categories' names) — none of them per-seat or per-tier.
        """
        VenueSeat.objects.create(sector=sector, label="anchor", default_price_category=standard)
        _make_tier(seated_venue_event, venue, sector, standard)

        def paint(n: int, category: PriceCategory) -> list[UUID]:
            seats = VenueSeat.objects.bulk_create(
                [VenueSeat(sector=sector, label=f"{category.name}-{n}-{i}") for i in range(n)]
            )
            return [s.id for s in seats]

        small = paint(2, balcony)
        with django_assert_num_queries(10):
            small_result = venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=small, price_category_id=balcony.id)
            )

        large = paint(200, balcony)
        with django_assert_num_queries(10):
            large_result = venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=large, price_category_id=balcony.id)
            )

        assert small_result.painted == 2
        assert large_result.painted == 200
        assert len(large_result.under_covered_tiers) == 1
