"""Lifecycle guards for category pricing (plan Task 12).

The map is JSON, so the database cannot protect it: no FK, no ``PROTECT``, no
cascade. Every invariant here is enforced by application code alone, which is why
each one is pinned by a test rather than trusted:

- **Deleting a price category** that a tier prices only through ``category_prices``
  must be refused. Nothing else stops it, and the failure is silent: the seats
  unpaint (``SET_NULL``) and a live on-sale reverts to the tier's flat price.
- **Duplication and recurrence** must carry the map. ``_duplicate_ticket_tiers``
  copies ``concrete_fields``, so it *should* come along for free — asserted, not
  assumed, because a prior field-enumeration bug is exactly why that function was
  rewritten.
- **Repainting** never fails (spec §4.3, amended 2026-07-20): ``paint_seats`` is
  venue-scoped, so one event's pricing config must not block map work for every
  other event at the venue. The gap it can open therefore has to bite at checkout
  (400 on the affected seat) and be visible to the admin (``pricing_gaps``).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from events import schema
from events.models import Event, EventSeries, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector
from events.service import recurrence_service, venue_service
from events.service.duplication import duplicate_event
from events.service.seating import pricing
from events.utils.tier_pricing import parse_price_map

pytestmark = pytest.mark.django_db

PREMIUM = Decimal("80.00")
STANDARD = Decimal("30.00")
FLAT = Decimal("50.00")


@pytest.fixture
def venue(organization: Organization) -> Venue:
    return Venue.objects.create(organization=organization, name="Theatre", capacity=100)


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Stalls")


@pytest.fixture
def premium(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=0)


@pytest.fixture
def standard(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa", display_order=1)


@pytest.fixture
def seats(sector: VenueSector, premium: PriceCategory, standard: PriceCategory) -> list[VenueSeat]:
    return [
        VenueSeat.objects.create(sector=sector, label="A1", row_label="A", number=1, default_price_category=premium),
        VenueSeat.objects.create(sector=sector, label="B1", row_label="B", number=1, default_price_category=standard),
    ]


@pytest.fixture
def seated_event(event: Event, venue: Venue) -> Event:
    event.venue = venue
    event.save(update_fields=["venue"])
    return event


@pytest.fixture
def tier(
    seated_event: Event,
    venue: Venue,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
    seats: list[VenueSeat],
) -> TicketTier:
    """A fully-covered category-priced user-choice tier."""
    return TicketTier.objects.create(
        event=seated_event,
        name="Stalls",
        price=FLAT,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        venue=venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        category_prices={str(premium.id): str(PREMIUM), str(standard.id): str(STANDARD)},
    )


class TestCategoryDeleteGuard:
    """The map-aware delete guard — the only defence there is."""

    def test_delete_refused_when_referenced_only_by_the_map(
        self, tier: TicketTier, premium: PriceCategory, seats: list[VenueSeat]
    ) -> None:
        """A category priced by a user-choice tier is not deletable, despite having no FK."""
        assert str(premium.id) in tier.category_prices  # referenced *only* through the JSON map

        with pytest.raises(HttpError) as exc_info:
            venue_service.delete_price_category(premium)

        assert exc_info.value.status_code == 400
        assert PriceCategory.objects.filter(id=premium.id).exists()
        # And the seats keep their paint — the silent repricing never starts.
        seats[0].refresh_from_db()
        assert seats[0].default_price_category_id == premium.id

    def test_refusal_names_the_offending_tiers(
        self, tier: TicketTier, seated_event: Event, premium: PriceCategory
    ) -> None:
        """The admin cannot fix a gap they cannot locate: the message names event and tier."""
        with pytest.raises(HttpError) as exc_info:
            venue_service.delete_price_category(premium)

        message = str(exc_info.value)
        assert "ticket tiers" in message
        assert tier.name in message
        assert seated_event.name in message

    def test_delete_allowed_when_no_tier_prices_the_category(
        self, tier: TicketTier, venue: Venue, sector: VenueSector
    ) -> None:
        """An unreferenced category still deletes, unpainting its seats."""
        unused = PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00")
        seat = VenueSeat.objects.create(sector=sector, label="C1", default_price_category=unused)

        venue_service.delete_price_category(unused)

        assert not PriceCategory.objects.filter(id=unused.id).exists()
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_guard_survives_a_tier_on_another_event_at_the_same_venue(
        self,
        tier: TicketTier,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """Categories are venue-scoped: any event's tier blocks the delete, not just this one."""
        other = Event.objects.create(
            organization=organization,
            name="Second Night",
            start=timezone.now() + timedelta(days=30),
            end=timezone.now() + timedelta(days=30, hours=2),
            venue=venue,
        )
        TicketTier.objects.create(
            event=other,
            name="Cheap Seats",
            price=FLAT,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            category_prices={str(premium.id): str(PREMIUM), str(standard.id): str(STANDARD)},
        )
        # Both events' tiers price `standard`; the refusal must name both.
        with pytest.raises(HttpError) as exc_info:
            venue_service.delete_price_category(standard)

        assert "Cheap Seats" in str(exc_info.value)
        assert "Second Night" in str(exc_info.value)

    def test_deleting_the_whole_venue_still_cascades(self, tier: TicketTier, venue: Venue) -> None:
        """Deleting a venue legitimately takes its categories with it — the guard is not a DB constraint.

        Pinned so the asymmetry is deliberate rather than discovered: the guard lives in
        the price-category service and does not (and should not) veto disposing of the
        venue the categories belong to.
        """
        venue.delete()

        assert not PriceCategory.objects.filter(venue_id=venue.id).exists()


class TestDuplicationCarriesTheMap:
    """Duplication and recurrence must not silently produce flat-priced copies."""

    def test_duplicate_event_copies_category_prices(
        self, tier: TicketTier, seated_event: Event, sector: VenueSector, seats: list[VenueSeat]
    ) -> None:
        """``_duplicate_ticket_tiers`` copies concrete fields — prove the map is one of them.

        Also pins the seating FKs. They used to be cleared per occurrence, which made a
        USER_CHOICE tier fail ``clean()`` on the copy: duplicating (or generating any
        recurring occurrence of) a reserved-seating event raised ``ValidationError``.
        """
        new_event = duplicate_event(seated_event, "Second Night", timezone.now() + timedelta(days=30))

        copy = new_event.ticket_tiers.get(name=tier.name)
        assert copy.pk != tier.pk
        assert copy.category_prices == tier.category_prices
        assert copy.venue_id == tier.venue_id
        assert copy.sector_id == sector.id
        # Not merely equal-looking JSON: it must resolve to real money.
        assert pricing.resolve_seat_price(copy, seats[0], parse_price_map(copy.category_prices)) == PREMIUM

    def test_recurring_occurrence_carries_the_map(
        self,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        premium: PriceCategory,
        standard: PriceCategory,
        seats: list[VenueSeat],
        active_series: EventSeries,
    ) -> None:
        """Occurrences are built through ``duplicate_event`` — the map must ride along."""
        template = active_series.template_event
        assert template is not None
        template.venue = venue
        template.save(update_fields=["venue"])
        template.ticket_tiers.all().delete()
        TicketTier.objects.create(
            event=template,
            name="Stalls",
            price=FLAT,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            category_prices={str(premium.id): str(PREMIUM), str(standard.id): str(STANDARD)},
        )

        occurrence = recurrence_service.materialize_occurrence(active_series, timezone.now() + timedelta(days=7), 1)

        occurrence_tier = occurrence.ticket_tiers.get(name="Stalls")
        assert parse_price_map(occurrence_tier.category_prices) == {premium.id: PREMIUM, standard.id: STANDARD}


class TestRepaintLifecycle:
    """Repainting never fails; the gap it can open surfaces at checkout and to the admin."""

    def test_repaint_into_a_priced_category_keeps_everything_consistent(
        self, tier: TicketTier, venue: Venue, seats: list[VenueSeat], premium: PriceCategory
    ) -> None:
        """Moving a seat between two priced categories just reprices it — loudly (#747)."""
        standard_seat = seats[1]
        result = venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[standard_seat.id], price_category_id=premium.id)
        )

        assert result.painted == 1
        # Coverage stays complete, so there is no gap — but the money moved, and that is
        # the whole point of the report: nothing else in the system would have said so.
        assert len(result.affected_tiers) == 1
        assert result.affected_tiers[0].missing_categories == []
        assert [(c.seat_count, c.from_price, c.to_price) for c in result.affected_tiers[0].price_changes] == [
            (1, STANDARD, PREMIUM)
        ]
        standard_seat.refresh_from_db()
        price_map = parse_price_map(tier.category_prices)
        assert pricing.resolve_seat_price(tier, standard_seat, price_map) == PREMIUM
        assert schema.TicketTierDetailSchema.resolve_pricing_gaps(tier) == []

    def test_repaint_into_an_unpriced_category_succeeds_but_makes_the_seat_unsellable(
        self, tier: TicketTier, venue: Venue, seats: list[VenueSeat]
    ) -> None:
        """Paint always wins; checkout refuses the affected seat and only that seat."""
        balcony = PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00", display_order=2)
        premium_seat, standard_seat = seats

        result = venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[standard_seat.id], price_category_id=balcony.id)
        )
        assert result.painted == 1

        standard_seat.refresh_from_db()
        price_map = parse_price_map(tier.category_prices)
        with pytest.raises(HttpError) as exc_info:
            pricing.resolve_seat_price(tier, standard_seat, price_map)
        assert exc_info.value.status_code == 400
        assert "Balcony" in str(exc_info.value)
        # The rest of the sector still sells.
        assert pricing.resolve_seat_price(tier, premium_seat, price_map) == PREMIUM

    def test_gap_is_surfaced_on_the_admin_tier_payload(
        self, tier: TicketTier, venue: Venue, seats: list[VenueSeat]
    ) -> None:
        """The admin has no other way to learn their tier stopped covering its sector."""
        balcony = PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00", display_order=2)
        venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seats[1].id], price_category_id=balcony.id)
        )

        gaps = schema.TicketTierDetailSchema.resolve_pricing_gaps(tier)

        assert [g.name for g in gaps] == ["Balcony"]
        assert gaps[0].id == balcony.id

    def test_partial_map_best_available_tier_reports_no_gaps(
        self, seated_event: Event, venue: Venue, sector: VenueSector, seats: list[VenueSeat]
    ) -> None:
        """v3: a best-available map names the tier's zones, so an unpriced painted category is no gap."""
        premium_seat, _standard_seat = seats
        premium_category_id = premium_seat.default_price_category_id
        assert premium_category_id is not None
        ba = TicketTier.objects.create(
            event=seated_event,
            name="Premium Only",
            price=FLAT,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            category_prices={str(premium_category_id): str(PREMIUM)},
        )

        assert schema.TicketTierDetailSchema.resolve_pricing_gaps(ba) == []

    def test_flat_tier_reports_no_gaps(self, seated_event: Event, venue: Venue, sector: VenueSector) -> None:
        """A tier with no map is not under-covered — it is flat-priced, which is legal."""
        flat = TicketTier.objects.create(
            event=seated_event,
            name="General",
            price=FLAT,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
        )

        assert schema.TicketTierDetailSchema.resolve_pricing_gaps(flat) == []

    def test_buyer_payload_flags_the_drifted_category_instead_of_quoting_it(
        self, tier: TicketTier, venue: Venue, seats: list[VenueSeat]
    ) -> None:
        """The window where a buyer is shown a price checkout would refuse must not exist."""
        balcony = PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00", display_order=2)
        venue_service.paint_seats(
            venue, schema.VenueSeatPaintSchema(seat_ids=[seats[1].id], price_category_id=balcony.id)
        )

        seat_pricing = schema.TicketTierSchema.resolve_seat_pricing(tier)

        assert seat_pricing is not None
        by_name = {c.name: c for c in seat_pricing.categories}
        assert by_name["Premium"].available is True
        assert by_name["Premium"].price == PREMIUM
        # Listed (so the frontend can grey the seats out) but never priced.
        assert by_name["Balcony"].available is False
        assert by_name["Balcony"].price is None
        assert seat_pricing.unpainted == FLAT
