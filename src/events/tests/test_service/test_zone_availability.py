"""Per-zone availability counts on the availability payload.

The contract these tests defend is *agreement*: whatever
``ZoneAvailabilitySchema.largest_contiguous_block`` reports, a best-available hold for
that quantity must succeed — and for one more, must fail. A zone picker that greys out
zones on a number the picker disagrees with is worse than no picker at all.
"""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeatOverride,
    Organization,
    PriceCategory,
    SeatHold,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.schema.seating import ZoneAvailabilitySchema
from events.service.seating import availability
from events.service.seating.pick import hold_best_available

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="zone_buyer@example.com", email="zone_buyer@example.com")


@pytest.fixture
def rival(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="zone_rival@example.com", email="zone_rival@example.com")


class ZonedVenue(t.NamedTuple):
    """A venue with two seated sectors and two categories, plus one unpainted sector.

    ``stalls`` row A: seats 0-5, front half painted PREMIUM, back half STANDARD.
    ``balcony`` row A: seats 0-3, all painted PREMIUM (same category, other sector).
    ``lounge``: 3 unpainted seats.
    """

    event: Event
    premium: PriceCategory
    standard: PriceCategory
    stalls: VenueSector
    balcony: VenueSector
    lounge: VenueSector
    stalls_premium: list[VenueSeat]
    stalls_standard: list[VenueSeat]
    balcony_premium: list[VenueSeat]
    lounge_seats: list[VenueSeat]


def _seats(sector: VenueSector, count: int, category: PriceCategory | None, *, prefix: str = "A") -> list[VenueSeat]:
    return [
        VenueSeat.objects.create(
            sector=sector,
            label=f"{prefix}{i + 1}",
            row_label="A",
            number=i + 1,
            row_order=0,
            adjacency_index=i,
            default_price_category=category,
        )
        for i in range(count)
    ]


@pytest.fixture
def zoned(event: Event, organization: Organization) -> ZonedVenue:
    venue = Venue.objects.create(organization=organization, name="Zoned Hall")
    premium = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=0)
    standard = PriceCategory.objects.create(venue=venue, name="Standard", color="#00aa00", display_order=1)
    stalls = VenueSector.objects.create(venue=venue, name="Stalls", display_order=0)
    balcony = VenueSector.objects.create(venue=venue, name="Balcony", display_order=1)
    lounge = VenueSector.objects.create(venue=venue, name="Lounge", display_order=2)

    stalls_premium = _seats(stalls, 3, premium)
    stalls_standard = [
        VenueSeat.objects.create(
            sector=stalls,
            label=f"A{i + 1}",
            row_label="A",
            number=i + 1,
            row_order=0,
            adjacency_index=i,
            default_price_category=standard,
        )
        for i in range(3, 6)
    ]
    balcony_premium = _seats(balcony, 4, premium)
    lounge_seats = _seats(lounge, 3, None)

    event.venue = venue
    event.max_tickets_per_user = None
    event.save(update_fields=["venue", "max_tickets_per_user"])
    return ZonedVenue(
        event=event,
        premium=premium,
        standard=standard,
        stalls=stalls,
        balcony=balcony,
        lounge=lounge,
        stalls_premium=stalls_premium,
        stalls_standard=stalls_standard,
        balcony_premium=balcony_premium,
        lounge_seats=lounge_seats,
    )


def _zone(
    payload_zones: list[ZoneAvailabilitySchema], sector: VenueSector, category: PriceCategory
) -> ZoneAvailabilitySchema:
    matches = [z for z in payload_zones if z.sector_id == sector.id and z.price_category_id == category.id]
    assert len(matches) == 1, f"expected exactly one row for {sector.name}/{category.name}, got {matches}"
    return matches[0]


def _build(event: Event) -> list[ZoneAvailabilitySchema]:
    return availability.build_availability(event, user=None, guest_session=None).zones


def _tier(zoned: ZonedVenue, sector: VenueSector, categories: list[PriceCategory]) -> TicketTier:
    return TicketTier.objects.create(
        event=zoned.event,
        name=f"BA {sector.name}",
        price=Decimal("10.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        category_prices={str(c.id): "10.00" for c in categories},
    )


def test_fresh_venue_reports_every_painted_pair(zoned: ZonedVenue) -> None:
    zones = _build(zoned.event)
    assert len(zones) == 3  # stalls/premium, stalls/standard, balcony/premium
    assert _zone(zones, zoned.stalls, zoned.premium).free_seats == 3
    assert _zone(zones, zoned.stalls, zoned.standard).free_seats == 3
    assert _zone(zones, zoned.balcony, zoned.premium).free_seats == 4


def test_unpainted_sector_is_absent(zoned: ZonedVenue) -> None:
    """A sector with no paint sells no zone, so it contributes no row."""
    zones = _build(zoned.event)
    assert all(z.sector_id != zoned.lounge.id for z in zones)


def test_category_painted_in_two_sectors_is_counted_per_sector(zoned: ZonedVenue) -> None:
    """Never a venue-wide 7 for Premium: each tier sells one sector's slice of it."""
    zones = _build(zoned.event)
    premium_rows = [z for z in zones if z.price_category_id == zoned.premium.id]
    assert sorted(z.free_seats for z in premium_rows) == [3, 4]


def test_zones_are_ordered_by_sector_then_category_display_order(zoned: ZonedVenue) -> None:
    zones = _build(zoned.event)
    assert [(z.sector_id, z.price_category_id) for z in zones] == [
        (zoned.stalls.id, zoned.premium.id),
        (zoned.stalls.id, zoned.standard.id),
        (zoned.balcony.id, zoned.premium.id),
    ]


def test_sold_held_blocked_and_inactive_all_reduce_the_count(
    zoned: ZonedVenue, buyer: RevelUser, rival: RevelUser
) -> None:
    now = timezone.now()
    tier = _tier(zoned, zoned.balcony, [zoned.premium])
    seats = zoned.balcony_premium
    Ticket.objects.create(
        event=zoned.event, tier=tier, user=rival, seat=seats[0], sector=zoned.balcony, guest_name="Sold"
    )
    SeatHold.objects.create(
        event=zoned.event, seat=seats[1], user=rival, acquired_at=now, expires_at=now + timedelta(minutes=5)
    )
    EventSeatOverride.objects.create(
        event=zoned.event, seat=seats[2], status=EventSeatOverride.OverrideStatus.KILLED, reason="broken"
    )
    seats[3].is_active = False
    seats[3].save(update_fields=["is_active"])

    zone = _zone(_build(zoned.event), zoned.balcony, zoned.premium)
    assert zone.free_seats == 0
    assert zone.largest_contiguous_block == 0


def test_a_sold_out_zone_is_still_reported_with_zeroes(zoned: ZonedVenue, rival: RevelUser) -> None:
    """The picker must be able to grey it out — omitting the row would hide it."""
    tier = _tier(zoned, zoned.stalls, [zoned.premium])
    for seat in zoned.stalls_premium:
        Ticket.objects.create(
            event=zoned.event, tier=tier, user=rival, seat=seat, sector=zoned.stalls, guest_name="Sold"
        )
    zone = _zone(_build(zoned.event), zoned.stalls, zoned.premium)
    assert (zone.free_seats, zone.largest_contiguous_block, zone.accessible_free_seats) == (0, 0, 0)


def test_expired_hold_does_not_reduce_the_count(zoned: ZonedVenue, rival: RevelUser) -> None:
    now = timezone.now()
    SeatHold.objects.create(
        event=zoned.event,
        seat=zoned.balcony_premium[0],
        user=rival,
        acquired_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(seconds=1),
    )
    assert _zone(_build(zoned.event), zoned.balcony, zoned.premium).free_seats == 4


def test_own_holds_count_as_taken_like_the_hold_path(zoned: ZonedVenue, buyer: RevelUser) -> None:
    """A caller's own hold is unavailable to a *new* best-available pick, which excludes
    every active hold regardless of owner. Reporting it free would over-promise; the
    caller still sees it in ``my_holds``."""
    now = timezone.now()
    SeatHold.objects.create(
        event=zoned.event,
        seat=zoned.balcony_premium[0],
        user=buyer,
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    payload = availability.build_availability(zoned.event, user=buyer, guest_session=None)
    zone = _zone(payload.zones, zoned.balcony, zoned.premium)
    assert zone.free_seats == 3
    assert payload.my_holds == [zoned.balcony_premium[0].id]


def test_accessible_seats_are_tallied_separately(zoned: ZonedVenue) -> None:
    """A general pick never falls back to accessible seats, so they must not inflate
    ``free_seats`` — otherwise the count promises seats no general hold can reach."""
    seat = zoned.balcony_premium[3]
    seat.is_accessible = True
    seat.save(update_fields=["is_accessible"])
    zone = _zone(_build(zoned.event), zoned.balcony, zoned.premium)
    assert zone.free_seats == 3
    assert zone.accessible_free_seats == 1
    assert zone.largest_contiguous_block == 3


def test_largest_contiguous_block_reflects_a_hole(zoned: ZonedVenue, rival: RevelUser) -> None:
    """Balcony premium is A1..A4; killing A2 leaves runs of 1 and 2."""
    EventSeatOverride.objects.create(
        event=zoned.event,
        seat=zoned.balcony_premium[1],
        status=EventSeatOverride.OverrideStatus.KILLED,
        reason="broken",
    )
    zone = _zone(_build(zoned.event), zoned.balcony, zoned.premium)
    assert zone.free_seats == 3
    assert zone.largest_contiguous_block == 2


@pytest.mark.parametrize("killed_index", [1, 2])
def test_reported_block_agrees_with_an_actual_hold(zoned: ZonedVenue, buyer: RevelUser, killed_index: int) -> None:
    """The point of the field: hold(q) succeeds iff largest_contiguous_block >= q."""
    EventSeatOverride.objects.create(
        event=zoned.event,
        seat=zoned.balcony_premium[killed_index],
        status=EventSeatOverride.OverrideStatus.KILLED,
        reason="broken",
    )
    tier = _tier(zoned, zoned.balcony, [zoned.premium])
    reported = _zone(_build(zoned.event), zoned.balcony, zoned.premium).largest_contiguous_block

    fits = hold_best_available(
        zoned.event, tier, reported, user=buyer, guest_session=None, price_category_id=zoned.premium.id
    )
    assert len(fits.held) == reported

    SeatHold.objects.filter(id__in=[h.id for h in fits.held]).delete()
    over = hold_best_available(
        zoned.event, tier, reported + 1, user=buyer, guest_session=None, price_category_id=zoned.premium.id
    )
    assert over.held == []  # the 409 "no adjacent block" path


def test_full_zone_hold_agrees_and_then_reports_zero(zoned: ZonedVenue, buyer: RevelUser) -> None:
    """Hold the whole reported block, re-poll, and the zone must now say nothing fits."""
    tier = _tier(zoned, zoned.balcony, [zoned.premium])
    before = _zone(_build(zoned.event), zoned.balcony, zoned.premium)
    assert before.largest_contiguous_block == 4

    result = hold_best_available(
        zoned.event, tier, 4, user=buyer, guest_session=None, price_category_id=zoned.premium.id
    )
    assert len(result.held) == 4

    after = _zone(_build(zoned.event), zoned.balcony, zoned.premium)
    assert (after.free_seats, after.largest_contiguous_block) == (0, 0)


def test_accessible_count_agrees_with_an_accessible_hold(zoned: ZonedVenue, buyer: RevelUser) -> None:
    """Accessible picks ignore adjacency: the count alone is the exact predicate."""
    for seat in zoned.balcony_premium[:2]:
        seat.is_accessible = True
        seat.save(update_fields=["is_accessible"])
    tier = _tier(zoned, zoned.balcony, [zoned.premium])
    reported = _zone(_build(zoned.event), zoned.balcony, zoned.premium).accessible_free_seats
    assert reported == 2

    fits = hold_best_available(
        zoned.event,
        tier,
        reported,
        user=buyer,
        guest_session=None,
        accessible_required=True,
        price_category_id=zoned.premium.id,
    )
    assert len(fits.held) == reported

    SeatHold.objects.filter(id__in=[h.id for h in fits.held]).delete()
    over = hold_best_available(
        zoned.event,
        tier,
        reported + 1,
        user=buyer,
        guest_session=None,
        accessible_required=True,
        price_category_id=zoned.premium.id,
    )
    assert over.held == []


def test_query_count_is_bounded_and_independent_of_zone_count(
    zoned: ZonedVenue, django_assert_num_queries: t.Any
) -> None:
    """This endpoint is polled: adding sectors/categories must not add queries."""
    with django_assert_num_queries(8):
        availability.build_availability(zoned.event, user=None, guest_session=None)

    venue = zoned.event.venue
    assert venue is not None
    for n in range(3):
        category = PriceCategory.objects.create(venue=venue, name=f"Extra {n}", color="#0000aa", display_order=10 + n)
        sector = VenueSector.objects.create(venue=venue, name=f"Wing {n}", display_order=10 + n)
        _seats(sector, 4, category, prefix=f"W{n}-")

    with django_assert_num_queries(8):
        payload = availability.build_availability(zoned.event, user=None, guest_session=None)
    assert len(payload.zones) == 6


def test_no_venue_reports_no_zones(event: Event) -> None:
    assert availability.build_availability(event, user=None, guest_session=None).zones == []


def test_longest_run_handles_an_empty_row() -> None:
    assert availability._longest_run([]) == 0
    assert availability._longest_run([5]) == 1
    assert availability._longest_run([0, 1, 3, 4, 5]) == 3


def test_seat_ids_are_not_leaked_into_the_zone_rows(zoned: ZonedVenue) -> None:
    """Zone rows carry counts only — identity comes from the chart, keyed by these ids."""
    row = _build(zoned.event)[0]
    assert set(row.model_dump()) == {
        "sector_id",
        "price_category_id",
        "free_seats",
        "largest_contiguous_block",
        "accessible_free_seats",
    }
    assert isinstance(row.sector_id, uuid.UUID)
