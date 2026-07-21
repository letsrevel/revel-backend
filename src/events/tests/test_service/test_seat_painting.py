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
        assert result.affected_tiers == []
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
DEFAULT_CATEGORY_PRICE = Decimal("80.00")


def _price_map(*categories: PriceCategory) -> dict[str, str]:
    return {str(c.id): str(DEFAULT_CATEGORY_PRICE) for c in categories}


def _make_tier(
    event: Event,
    venue: Venue,
    sector: VenueSector,
    *categories: PriceCategory,
    prices: dict[PriceCategory, str] | None = None,
    name: str = "Stalls",
    mode: str = TicketTier.SeatAssignmentMode.USER_CHOICE,
) -> TicketTier:
    """A tier on ``sector``, category-priced for ``categories`` (none = flat).

    ``prices`` gives explicit per-category amounts; without it every listed category
    costs :data:`DEFAULT_CATEGORY_PRICE`.
    """
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=FLAT,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        venue=venue,
        sector=sector,
        seat_assignment_mode=mode,
        category_prices={str(c.id): p for c, p in prices.items()} if prices else _price_map(*categories),
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


def _paint(
    venue: Venue, seats: list[VenueSeat], category: PriceCategory | None, *, preview: bool = False
) -> schema.SeatPaintResultSchema:
    return venue_service.paint_seats(
        venue,
        schema.VenueSeatPaintSchema(
            seat_ids=[s.id for s in seats],
            price_category_id=category.id if category else None,
        ),
        preview=preview,
    )


@pytest.fixture
def gallery(venue: Venue) -> PriceCategory:
    """A category no tier prices — the under-coverage half of the report."""
    return PriceCategory.objects.create(venue=venue, name="Gallery", color="#aa00aa", display_order=3)


@pytest.fixture
def matching(venue: Venue) -> PriceCategory:
    """A category priced at exactly the tier's flat price: painted, but not a repricing."""
    return PriceCategory.objects.create(venue=venue, name="Matching", color="#555555", display_order=4)


PREMIUM_PRICE = Decimal("80.00")
STANDARD_PRICE = Decimal("30.00")


@pytest.fixture
def priced_tier(
    seated_venue_event: Event,
    venue: Venue,
    sector: VenueSector,
    category: PriceCategory,
    standard: PriceCategory,
    matching: PriceCategory,
) -> TicketTier:
    """Premium 80, Standard 30, Matching 50 (== flat). Balcony/Gallery unpriced."""
    return _make_tier(
        seated_venue_event,
        venue,
        sector,
        prices={category: str(PREMIUM_PRICE), standard: str(STANDARD_PRICE), matching: str(FLAT)},
    )


class TestPaintAffectedTierReport:
    """Paint always succeeds; what it did to the money comes back in the response.

    Two failure modes, one report. Under-coverage **fails closed** (the seat stops selling
    and the buyer gets a 400) — loud, someone notices. Repainting between two *priced*
    categories **fails open**: sales continue at the wrong price, for every event at the
    venue, with every other signal in the system silent because no price is missing. The
    report is advisory and deliberately quiet: it must stay empty when nothing moved, or
    the frontend warning it feeds becomes noise the admin learns to dismiss.
    """

    @pytest.fixture
    def categories(
        self,
        category: PriceCategory,
        standard: PriceCategory,
        balcony: PriceCategory,
        gallery: PriceCategory,
        matching: PriceCategory,
    ) -> dict[str, PriceCategory]:
        return {
            "premium": category,
            "standard": standard,
            "balcony": balcony,
            "gallery": gallery,
            "matching": matching,
        }

    @pytest.mark.parametrize(
        ("start", "target", "expected_changes", "expected_missing"),
        [
            # --- no-ops: nothing moved, nothing to say ---
            pytest.param("premium", "premium", [], [], id="repaint-same-category-is-a-no-op"),
            pytest.param(None, None, [], [], id="unpaint-an-unpainted-seat-is-a-no-op"),
            pytest.param(None, "matching", [], [], id="paint-into-a-category-priced-at-the-flat-price"),
            pytest.param("balcony", "gallery", [], ["Gallery"], id="unpriced-to-unpriced-moves-no-money"),
            # --- the silent repricings this report exists for ---
            pytest.param("premium", "standard", [(1, PREMIUM_PRICE, STANDARD_PRICE)], [], id="priced-to-priced"),
            pytest.param(None, "premium", [(1, FLAT, PREMIUM_PRICE)], [], id="unpainted-to-priced"),
            pytest.param("premium", None, [(1, PREMIUM_PRICE, FLAT)], [], id="unpaint-falls-back-to-flat"),
            # --- crossing the sellable boundary (None == checkout refuses the seat) ---
            pytest.param("premium", "balcony", [(1, PREMIUM_PRICE, None)], ["Balcony"], id="priced-to-unpriced"),
            pytest.param(None, "balcony", [(1, FLAT, None)], ["Balcony"], id="unpainted-to-unpriced"),
            pytest.param("balcony", "standard", [(1, None, STANDARD_PRICE)], [], id="unpriced-to-priced-recovers"),
            pytest.param("balcony", None, [(1, None, FLAT)], [], id="unpaint-an-unpriced-seat-recovers"),
        ],
    )
    def test_every_transition_lands_in_the_right_bucket(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        categories: dict[str, PriceCategory],
        start: str | None,
        target: str | None,
        expected_changes: list[tuple[int, Decimal | None, Decimal | None]],
        expected_missing: list[str],
    ) -> None:
        """The full from-category → to-category table, against one tier."""
        seat = VenueSeat.objects.create(
            sector=sector, label="A1", default_price_category=categories[start] if start else None
        )

        result = _paint(venue, [seat], categories[target] if target else None)

        assert result.painted == 1
        if not expected_changes and not expected_missing:
            assert result.affected_tiers == []
            return
        reported = result.affected_tiers[0]
        assert reported.tier_id == priced_tier.id
        assert [(c.seat_count, c.from_price, c.to_price) for c in reported.price_changes] == expected_changes
        assert [c.name for c in reported.missing_categories] == expected_missing

    def test_one_paint_reports_every_price_it_moved_seats_away_from(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A paint writes one category but overwrites many, so ``to`` is one price and ``from`` is not."""
        premium_seats = [
            VenueSeat.objects.create(sector=sector, label=f"P{i}", default_price_category=category) for i in range(3)
        ]
        unpainted = VenueSeat.objects.create(sector=sector, label="U1")

        result = _paint(venue, [*premium_seats, unpainted], standard)

        # Biggest move first, so the frontend can lead with the headline.
        assert [(c.seat_count, c.from_price, c.to_price) for c in result.affected_tiers[0].price_changes] == [
            (3, PREMIUM_PRICE, STANDARD_PRICE),
            (1, FLAT, STANDARD_PRICE),
        ]

    def test_inactive_seats_are_not_counted_as_repriced(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A decommissioned seat cannot be sold, so its price cannot have moved."""
        active = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        inactive = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=category, is_active=False)

        result = _paint(venue, [active, inactive], standard)

        assert result.painted == 2
        assert [c.seat_count for c in result.affected_tiers[0].price_changes] == [1]

    def test_report_names_the_event_and_its_status(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        seated_venue_event: Event,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The frontend deep-links to the event and ranks a live on-sale above a draft."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)

        reported = _paint(venue, [seat], standard).affected_tiers[0]

        assert (reported.tier_name, reported.event_id, reported.event_name, reported.event_status) == (
            "Stalls",
            seated_venue_event.id,
            seated_venue_event.name,
            seated_venue_event.status,
        )

    def test_a_paint_on_another_sector_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        standard: PriceCategory,
    ) -> None:
        """A tier selling a sector the paint never touched is unaffected by it."""
        other_sector = VenueSector.objects.create(venue=venue, name="Balcony Sector")
        seat = VenueSeat.objects.create(sector=other_sector, label="B1")

        assert _paint(venue, [seat], standard).affected_tiers == []

    def test_flat_tier_is_not_reported(
        self, venue: Venue, sector: VenueSector, seated_venue_event: Event, standard: PriceCategory
    ) -> None:
        """The common case: a tier with no price map charges its flat price whatever is painted."""
        _make_tier(seated_venue_event, venue, sector, name="General")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        assert _paint(venue, [seat], standard).affected_tiers == []

    def test_best_available_tier_is_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        standard: PriceCategory,
        balcony: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """v3 inverts this: both seated modes read the map, so both are repriceable by a paint."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        _make_tier(
            seated_venue_event,
            venue,
            sector,
            prices={standard: str(FLAT)},
            name="Best",
            mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )

        assert _paint(venue, [seat], balcony).affected_tiers != []

    def test_best_available_tier_reprices_like_a_user_choice_one(
        self,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
        standard: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """A move between two zones the map prices is the silent case the report exists for."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        tier = _make_tier(
            seated_venue_event,
            venue,
            sector,
            prices={category: str(PREMIUM_PRICE), standard: str(STANDARD_PRICE)},
            name="Best",
            mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )

        reported = _paint(venue, [seat], standard).affected_tiers

        assert [r.tier_id for r in reported] == [tier.id]
        assert [(c.seat_count, c.from_price, c.to_price) for c in reported[0].price_changes] == [
            (1, PREMIUM_PRICE, STANDARD_PRICE)
        ]

    def test_best_available_partial_map_reports_no_missing_categories(
        self,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
        balcony: PriceCategory,
        gallery: PriceCategory,
        seated_venue_event: Event,
    ) -> None:
        """The map keys *are* the tier's zones, so a category it never priced is not a gap.

        Same rule as ``resolve_pricing_gaps`` and write-time validation: advising an
        organizer to price a category they deliberately left out of the pool would be a
        permanent false alarm on every paint of that sector.
        """
        priced_seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        drifting = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=balcony)
        _make_tier(
            seated_venue_event,
            venue,
            sector,
            prices={category: str(PREMIUM_PRICE)},
            name="Best",
            mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )

        # Moving between two unpriced categories touches neither the pool nor the money.
        assert _paint(venue, [drifting], gallery).affected_tiers == []
        # Leaving the pool is still reported — as a price change, never as a gap.
        reported = _paint(venue, [priced_seat], gallery).affected_tiers
        assert reported[0].missing_categories == []
        assert [(c.seat_count, c.from_price, c.to_price) for c in reported[0].price_changes] == [
            (1, PREMIUM_PRICE, None)
        ]

    def test_past_event_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        seated_venue_event: Event,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """Nobody can sell a finished event's seats, so its prices are noise."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        now = timezone.now()
        Event.objects.filter(pk=seated_venue_event.pk).update(
            start=now - timedelta(days=3), end=now - timedelta(days=2)
        )

        assert _paint(venue, [seat], standard).affected_tiers == []

    def test_cancelled_event_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        seated_venue_event: Event,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        Event.objects.filter(pk=seated_venue_event.pk).update(status=Event.EventStatus.CANCELLED)

        assert _paint(venue, [seat], standard).affected_tiers == []

    def test_malformed_price_map_is_skipped_not_raised(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A legacy map that will not parse must never turn a paint into a 500 (spec §4.3)."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        TicketTier.objects.filter(pk=priced_tier.pk).update(category_prices={"not-a-uuid": "nope"})

        result = _paint(venue, [seat], standard)

        assert result.painted == 1
        assert result.affected_tiers == []

    def test_gap_is_the_current_state_price_change_is_the_delta(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        balcony: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A gap this paint did not open is still a gap; a price this paint did not move is not news."""
        stuck = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        other = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)
        assert _paint(venue, [stuck], balcony).affected_tiers[0].price_changes  # opens the gap

        # Repainting an unrelated seat into the category it already has moves nothing —
        # but A1 is still refused at checkout, so the tier is still reported.
        result = _paint(venue, [other], standard)

        assert result.affected_tiers[0].price_changes == []
        assert [c.name for c in result.affected_tiers[0].missing_categories] == ["Balcony"]

    def test_query_count_does_not_scale_with_seats_painted(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        balcony: PriceCategory,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Painting hundreds of seats must cost the same as painting two.

        Capturing the seats' prices *before* the UPDATE is the expensive-looking part of
        #747, and it must not become a per-seat read: one grouped query bounded by
        (sector × category × is_active) serves the 404 check, the touched sectors, and the
        prior paint state all at once — one query fewer than #746 needed for the first two.

        The tenth query is the chart-version bump (#752): one UPDATE on one venue row,
        flat in the number of seats painted like everything else here.
        """
        VenueSeat.objects.create(sector=sector, label="anchor", default_price_category=category)

        def make(n: int, prefix: str) -> list[UUID]:
            seats = VenueSeat.objects.bulk_create(
                [VenueSeat(sector=sector, label=f"{prefix}-{i}", default_price_category=category) for i in range(n)]
            )
            return [s.id for s in seats]

        small = make(2, "small")
        with django_assert_num_queries(10):
            small_result = venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=small, price_category_id=balcony.id)
            )

        large = make(200, "large")
        with django_assert_num_queries(10):
            large_result = venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=large, price_category_id=balcony.id)
            )

        assert (small_result.painted, large_result.painted) == (2, 200)
        assert [(c.seat_count, c.from_price, c.to_price) for c in large_result.affected_tiers[0].price_changes] == [
            (200, PREMIUM_PRICE, None)
        ]
        assert [c.name for c in large_result.affected_tiers[0].missing_categories] == ["Balcony"]


class TestPaintPreview:
    """``preview=True``: the same answer, in advance, with nothing written.

    The report has to be computed before the UPDATE anyway (the UPDATE overwrites the
    categories it reports on), so the preview is the same code path with the write
    skipped. These tests exist to keep it that way: a preview that could disagree with
    the paint would let an admin confirm one repricing and get another.
    """

    def test_preview_payload_is_identical_to_the_real_paint(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        balcony: PriceCategory,
    ) -> None:
        """Whole-payload equality, not spot checks: this is the promise the button makes.

        The paint deliberately *opens* a coverage gap, because that is the half of the
        report that reads the sector's current state — a preview that simply skipped the
        UPDATE and re-read would report the pre-paint gap and be wrong here.
        """
        seats = [
            VenueSeat.objects.create(sector=sector, label=f"A{i}", default_price_category=category) for i in range(3)
        ]

        previewed = _paint(venue, seats, balcony, preview=True)
        real = _paint(venue, seats, balcony)

        assert previewed.model_dump() == real.model_dump()
        assert [c.name for c in previewed.affected_tiers[0].missing_categories] == ["Balcony"]
        assert previewed.affected_tiers[0].price_changes, "the case must be non-trivial, or this proves nothing"

    def test_preview_of_a_paint_that_closes_the_last_gap_reports_it_closed(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        balcony: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The other direction: the gap the paint removes must be gone in the preview too."""
        seats = [
            VenueSeat.objects.create(sector=sector, label=f"A{i}", default_price_category=balcony) for i in range(2)
        ]

        previewed = _paint(venue, seats, standard, preview=True)
        real = _paint(venue, seats, standard)

        assert previewed.model_dump() == real.model_dump()
        assert previewed.affected_tiers[0].missing_categories == []

    def test_preview_then_real_reports_the_same_thing_twice_and_only_then_paints(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The actual flow: preview, admin confirms, paint. The confirmation must hold."""
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)

        previewed = _paint(venue, [seat], standard, preview=True)
        seat.refresh_from_db()
        assert seat.default_price_category_id == category.id

        real = _paint(venue, [seat], standard)

        assert previewed.model_dump() == real.model_dump()
        assert [(c.seat_count, c.from_price, c.to_price) for c in real.affected_tiers[0].price_changes] == [
            (1, PREMIUM_PRICE, STANDARD_PRICE)
        ]
        seat.refresh_from_db()
        assert seat.default_price_category_id == standard.id

    def test_preview_writes_nothing_not_even_updated_at(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        balcony: PriceCategory,
    ) -> None:
        """``paint_seats`` stamps ``updated_at`` on purpose (the chart version, and therefore
        the buyer's poller, is derived from it). A preview must not move it — a dry run that
        invalidated every open seat chart would be a write in all the ways that matter."""
        seats = [
            VenueSeat.objects.create(sector=sector, label=f"A{i}", default_price_category=category) for i in range(3)
        ]
        before = {s.id: (s.default_price_category_id, s.updated_at) for s in VenueSeat.objects.all()}

        _paint(venue, seats, balcony, preview=True)

        after = {s.id: (s.default_price_category_id, s.updated_at) for s in VenueSeat.objects.all()}
        assert after == before

    def test_preview_still_404s_on_a_foreign_seat_and_writes_nothing(
        self, venue: Venue, sector: VenueSector, organization: Organization, category: PriceCategory
    ) -> None:
        """Previewing a paint that would fail must fail — the alternative is a confirmed lie."""
        other = Venue.objects.create(organization=organization, name="Other Hall")
        other_sector = VenueSector.objects.create(venue=other, name="Foreign")
        foreign_seat = VenueSeat.objects.create(sector=other_sector, label="X1")
        mine = VenueSeat.objects.create(sector=sector, label="A1")

        with pytest.raises(HttpError) as exc_info:
            _paint(venue, [mine, foreign_seat], category, preview=True)

        assert exc_info.value.status_code == 404
        for seat in (mine, foreign_seat):
            seat.refresh_from_db()
            assert seat.default_price_category_id is None

    def test_preview_still_400s_on_a_foreign_category_and_writes_nothing(
        self, venue: Venue, sector: VenueSector, other_venue_category: PriceCategory
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        with pytest.raises(HttpError) as exc_info:
            _paint(venue, [seat], other_venue_category, preview=True)

        assert exc_info.value.status_code == 400
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_preview_is_one_query_cheaper_and_does_not_scale_with_seats(
        self,
        venue: Venue,
        sector: VenueSector,
        priced_tier: TicketTier,
        category: PriceCategory,
        balcony: PriceCategory,
        django_assert_num_queries: t.Any,
    ) -> None:
        """Preview = the real paint (9) minus the UPDATE, at any size.

        Flat in the number of seats for the same reason the paint is: the prior-state read
        is grouped by (sector × category × is_active), never per seat.
        """
        VenueSeat.objects.create(sector=sector, label="anchor", default_price_category=category)

        def make(n: int, prefix: str) -> list[UUID]:
            seats = VenueSeat.objects.bulk_create(
                [VenueSeat(sector=sector, label=f"{prefix}-{i}", default_price_category=category) for i in range(n)]
            )
            return [s.id for s in seats]

        small = make(2, "small")
        with django_assert_num_queries(8):
            small_result = venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=small, price_category_id=balcony.id), preview=True
            )

        large = make(200, "large")
        with django_assert_num_queries(8):
            large_result = venue_service.paint_seats(
                venue, schema.VenueSeatPaintSchema(seat_ids=large, price_category_id=balcony.id), preview=True
            )

        assert (small_result.painted, large_result.painted) == (2, 200)
        assert VenueSeat.objects.filter(default_price_category=balcony).count() == 0
