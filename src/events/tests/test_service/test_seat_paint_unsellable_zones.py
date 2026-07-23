"""The paint advisory's third signal: zones a paint left a best-available tier unable to fill.

*Priced-but-unpainted* is the converse of a pricing gap, and unlike its counterpart it is
**never** deliberate: ``resolve_requested_zone`` accepts any map key, ``load_candidates``
finds nothing to fill it with, and every buyer who picks that zone gets a 409 with nothing
explaining why. The condition already had a home on the tier screen
(``TicketTierDetailSchema.unsellable_zones``); what it lacked was a voice on the *venue*
screen, where the unpaint that causes it is performed. These tests pin that voice — and pin
that it stays quiet everywhere the tier screen is quiet, because the two share one rule
(``tier_pricing.unsellable_zone_ids``).
"""

from decimal import Decimal

import pytest

from events import schema
from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector
from events.service import venue_service

pytestmark = pytest.mark.django_db

FLAT = Decimal("50.00")


@pytest.fixture
def venue(organization: Organization) -> Venue:
    return Venue.objects.create(organization=organization, name="Main Hall")


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Stalls")


@pytest.fixture
def premium(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=1)


@pytest.fixture
def standard(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa", display_order=2)


@pytest.fixture
def seated_event(event: Event, venue: Venue) -> Event:
    event.venue = venue
    event.save(update_fields=["venue"])
    return event


def _tier(
    event: Event,
    venue: Venue,
    sector: VenueSector,
    prices: dict[PriceCategory, str],
    *,
    mode: str = TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    name: str = "Stalls",
) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=FLAT,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        venue=venue,
        sector=sector,
        seat_assignment_mode=mode,
        category_prices={str(c.id): p for c, p in prices.items()},
    )


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


class TestPaintUnsellableZoneReport:
    """Painting always succeeds; a zone it stranded comes back in the response."""

    def test_unpainting_the_last_seat_of_a_zone_names_it(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The main cause: ``price_category_id = null`` empties a zone the tier still sells."""
        tier = _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)

        result = _paint(venue, [last_premium], None)

        assert [(e.tier_id, [z.name for z in e.zones]) for e in result.unsellable_zone_tiers] == [
            (tier.id, ["Premium"])
        ]

    def test_repainting_the_last_seat_into_another_zone_names_it(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The other cause: the seat is still painted, just no longer with that category."""
        tier = _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)

        result = _paint(venue, [last_premium], standard)

        assert [(e.tier_id, [z.name for z in e.zones]) for e in result.unsellable_zone_tiers] == [
            (tier.id, ["Premium"])
        ]

    def test_a_paint_that_leaves_every_zone_filled_says_nothing(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """No cry-wolf: another seat still carries Premium, so the zone is still fillable."""
        _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        one_of_two = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A3", default_price_category=standard)

        result = _paint(venue, [one_of_two], standard)

        assert result.unsellable_zone_tiers == []

    def test_a_user_choice_tier_is_never_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A user-choice buyer picks seats, not zones, so an unpainted key costs nobody a 409.

        Identical setup to the first test but for the mode — and the *other* advisory still
        fires, which is what proves the silence is the rule and not an empty fixture.
        """
        _tier(
            seated_event,
            venue,
            sector,
            {premium: "80.00", standard: "30.00"},
            mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        )
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)

        result = _paint(venue, [last_premium], None)

        assert result.unsellable_zone_tiers == []
        assert result.affected_tiers[0].price_changes, "the paint must be non-trivial, or this proves nothing"

    def test_painted_but_unpriced_stays_unreported_on_a_best_available_tier(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The false alarm this must never resurrect: the map's keys scope the tier on purpose.

        Painting a seat into Standard, which the tier does not price, takes the seat out of
        the tier's pool by design. Premium is still painted elsewhere, so nothing is stranded.
        """
        _tier(seated_event, venue, sector, {premium: "80.00"})
        VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        seat = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=premium)

        result = _paint(venue, [seat], standard)

        assert result.unsellable_zone_tiers == []
        assert result.affected_tiers[0].missing_categories == []

    def test_unpainting_the_whole_sector_says_nothing(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A sector with no paint left is the mid-setup state, not a contradiction.

        Same guard as the tier screen: prices first, paint second is a supported ordering, so
        a bare sector must not light up every zone the tier prices.
        """
        _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        seats = [
            VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium),
            VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard),
        ]

        result = _paint(venue, seats, None)

        assert result.unsellable_zone_tiers == []

    def test_a_tier_this_paint_did_not_reprice_is_still_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """Why this is its own list: Premium is priced at the flat price, so no money moved.

        The unpaint falls back to ``tier.price`` — the same number — so ``price_changes`` is
        empty and a best-available tier reports no ``missing_categories`` either. The tier is
        therefore absent from ``affected_tiers`` while its Premium zone is now unfillable.
        """
        tier = _tier(seated_event, venue, sector, {premium: str(FLAT), standard: "30.00"})
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)

        result = _paint(venue, [last_premium], None)

        assert result.affected_tiers == []
        assert [z.name for z in result.unsellable_zone_tiers[0].zones] == ["Premium"]
        assert result.unsellable_zone_tiers[0].tier_id == tier.id

    def test_the_zone_list_is_the_current_state_not_this_paint_s_delta(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """A zone an *earlier* paint stranded is still unfillable, so it is still reported.

        The deliberate choice (same tense as ``missing_categories``): reporting only what this
        paint newly broke would go silent on the next unrelated paint of the same sector while
        every buyer picking Premium still 409s — teaching the organizer that silence is health.
        """
        _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        other = VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)
        assert _paint(venue, [last_premium], None).unsellable_zone_tiers  # strands Premium

        # An unrelated no-op repaint of another seat: this paint strands nothing at all.
        result = _paint(venue, [other], standard)

        assert result.affected_tiers == []
        assert [z.name for z in result.unsellable_zone_tiers[0].zones] == ["Premium"]

    def test_a_tier_on_an_untouched_sector_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """Scope: the advisory is about the sectors this paint touched, nothing else."""
        balcony = VenueSector.objects.create(venue=venue, name="Balcony")
        _tier(seated_event, venue, balcony, {premium: "80.00", standard: "30.00"}, name="Balcony")
        VenueSeat.objects.create(sector=balcony, label="B1", default_price_category=standard)
        stalls_seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)

        result = _paint(venue, [stalls_seat], None)

        assert result.unsellable_zone_tiers == []

    def test_a_cancelled_event_is_not_reported(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """Nobody can sell those seats anyway — the same scope the repricing half uses."""
        _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        Event.objects.filter(pk=seated_event.pk).update(status=Event.EventStatus.CANCELLED)
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)

        result = _paint(venue, [last_premium], None)

        assert result.unsellable_zone_tiers == []


class TestUnsellableZonePreviewParity:
    """``preview=True`` must return the *same object*, not a similar one.

    The dry run is what an organizer clicks to confirm a repricing before money moves. This
    signal is derived from the same UPDATE-independent painted set as the coverage half, so a
    preview cannot report the pre-paint zones — these tests are what keeps it that way.
    """

    def test_preview_and_real_paint_return_identical_reports(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """Whole-schema equality on a paint that strands a zone *and* moves money.

        A preview that skipped the write and re-read the sector would still see the Premium
        seat and report nothing — so the non-triviality assertions below are load-bearing.
        """
        _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)

        previewed = _paint(venue, [last_premium], None, preview=True)
        real = _paint(venue, [last_premium], None)

        assert previewed == real
        assert [z.name for z in previewed.unsellable_zone_tiers[0].zones] == ["Premium"]
        assert previewed.affected_tiers[0].price_changes, "the case must be non-trivial, or this proves nothing"

    def test_preview_of_a_paint_that_refills_a_zone_reports_it_healed(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The other direction: the zone the paint *fills* must be gone from the preview too.

        Premium is unfillable before this paint and fillable after it. A preview that re-read
        the sector would still report Premium and scare an organizer off the fix.
        """
        tier = _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=standard)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)
        assert schema.TicketTierDetailSchema.resolve_unsellable_zones(tier), (
            "the zone must be broken to begin with, or this proves nothing"
        )

        previewed = _paint(venue, [seat], premium, preview=True)
        real = _paint(venue, [seat], premium)

        assert previewed == real
        assert previewed.unsellable_zone_tiers == []

    def test_preview_writes_nothing_while_reporting_the_zone(
        self,
        venue: Venue,
        sector: VenueSector,
        seated_event: Event,
        premium: PriceCategory,
        standard: PriceCategory,
    ) -> None:
        """The advisory must not cost the seat its paint — a dry run is a read."""
        _tier(seated_event, venue, sector, {premium: "80.00", standard: "30.00"})
        last_premium = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=premium)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)

        result = _paint(venue, [last_premium], None, preview=True)

        assert result.unsellable_zone_tiers
        last_premium.refresh_from_db()
        assert last_premium.default_price_category_id == premium.id
