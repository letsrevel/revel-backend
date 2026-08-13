"""Tests for ``BatchTicketService.resolve_cart_seats`` — cross-group seat resolution (#846).

Cart-level entry point covering:
- USER_CHOICE seats across every group in the cart, locked PK-ordered in a SINGLE
  query (the deadlock fix — see ``seats.py``'s module docstring: two tiers sharing
  a sector, locked per-group instead, is a classic lock-order inversion).
- BEST_AVAILABLE groups resolving sequentially through the existing pick/lock/retry
  machinery.
- NONE groups needing no locking at all.

Called directly on a cart-form service — multi-group ``create_batch`` isn't wired up
until the cart-engine task (Task 7), so this is the only way to exercise multi-group
seat behavior for now.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    PriceCategory,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.batch_ticket_service.context import CartGroup

pytestmark = pytest.mark.django_db


@pytest.fixture
def cart_event(organization: Organization) -> Event:
    """Future-dated public event used across cart seat-resolution tests."""
    return Event.objects.create(
        organization=organization,
        name="Cart Test Event",
        slug="cart-test-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=10,
    )


@pytest.fixture
def venue(organization: Organization) -> Venue:
    """A venue shared by every tier in these tests."""
    return Venue.objects.create(organization=organization, name="Cart Venue", capacity=100)


def _item(seat: VenueSeat | None = None, guest_name: str = "Guest") -> TicketPurchaseItem:
    return TicketPurchaseItem(guest_name=guest_name, seat_id=seat.id if seat else None)


def _uc_tier(event: Event, venue: Venue, sector: VenueSector, name: str) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("50.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        venue=venue,
        sector=sector,
    )


def _make_seats(sector: VenueSector, count: int, offset: int = 0) -> list[VenueSeat]:
    return [
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{offset + i}",
            row_label="A",
            number=offset + i,
            adjacency_index=offset + i - 1,
            is_active=True,
        )
        for i in range(1, count + 1)
    ]


class TestSharedSectorCrossGroupLock:
    """Two USER_CHOICE tiers sharing a sector — the deadlock scenario the single
    cross-group lock query exists to prevent.
    """

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="Shared Sector")

    @pytest.fixture
    def seats(self, sector: VenueSector) -> list[VenueSeat]:
        return _make_seats(sector, 4)

    @pytest.fixture
    def tier_a(self, cart_event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        return _uc_tier(cart_event, venue, sector, "Tier A")

    @pytest.fixture
    def tier_b(self, cart_event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        return _uc_tier(cart_event, venue, sector, "Tier B")

    def test_single_lock_query_resolves_both_groups(
        self,
        cart_event: Event,
        tier_a: TicketTier,
        tier_b: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """One FOR UPDATE query locks all four seats; both groups get their seats."""
        group_a = CartGroup(tier=tier_a, items=[_item(seats[0]), _item(seats[1])])
        group_b = CartGroup(tier=tier_b, items=[_item(seats[2]), _item(seats[3])])
        service = BatchTicketService(cart_event, user=member_user, groups=[group_a, group_b])

        with CaptureQueriesContext(connection) as ctx:
            resolved = service.resolve_cart_seats([group_a, group_b])

        lock_queries = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        assert len(lock_queries) == 1

        assert resolved[0] == [seats[0], seats[1]]
        assert resolved[1] == [seats[2], seats[3]]


class TestMissingSeatId:
    """A USER_CHOICE group with an item missing ``seat_id`` is refused."""

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="Sector")

    @pytest.fixture
    def tier(self, cart_event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        return _uc_tier(cart_event, venue, sector, "UC Tier")

    def test_raises_400_when_seat_id_missing(self, cart_event: Event, tier: TicketTier, member_user: RevelUser) -> None:
        group = CartGroup(tier=tier, items=[_item(guest_name="Guest 1")])
        service = BatchTicketService(cart_event, user=member_user, groups=[group])

        with pytest.raises(HttpError) as exc_info:
            service.resolve_cart_seats([group])

        assert exc_info.value.status_code == 400
        assert "Seat selection is required" in str(exc_info.value.message)


class TestWrongSector:
    """A seat that exists but belongs to a different sector than the requesting
    group's tier is refused — even though the shared lock query (which doesn't
    filter by sector) successfully locks it.
    """

    @pytest.fixture
    def sector_a(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="Sector A")

    @pytest.fixture
    def sector_b(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="Sector B")

    @pytest.fixture
    def tier_a(self, cart_event: Event, venue: Venue, sector_a: VenueSector) -> TicketTier:
        return _uc_tier(cart_event, venue, sector_a, "Tier A")

    @pytest.fixture
    def seat_in_b(self, sector_b: VenueSector) -> VenueSeat:
        return _make_seats(sector_b, 1)[0]

    def test_raises_400_for_seat_in_wrong_sector(
        self,
        cart_event: Event,
        tier_a: TicketTier,
        seat_in_b: VenueSeat,
        member_user: RevelUser,
    ) -> None:
        group = CartGroup(tier=tier_a, items=[_item(seat_in_b)])
        service = BatchTicketService(cart_event, user=member_user, groups=[group])

        with pytest.raises(HttpError) as exc_info:
            service.resolve_cart_seats([group])

        assert exc_info.value.status_code == 400
        assert "not in the correct sector" in str(exc_info.value.message)


class TestBestAvailablePlusNone:
    """A BEST_AVAILABLE group and a NONE group in the same cart."""

    @pytest.fixture(autouse=True)
    def _event_venue(self, cart_event: Event, venue: Venue) -> None:
        """The BEST_AVAILABLE picker's candidate loader requires event.venue_id."""
        cart_event.venue = venue
        cart_event.save(update_fields=["venue"])

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="BA Sector")

    @pytest.fixture
    def category(self, venue: Venue) -> PriceCategory:
        return PriceCategory.objects.create(venue=venue, name="Std", color="#00aa00")

    @pytest.fixture
    def ba_seats(self, sector: VenueSector, category: PriceCategory) -> list[VenueSeat]:
        seats = _make_seats(sector, 2)
        VenueSeat.objects.filter(id__in=[s.id for s in seats]).update(default_price_category=category)
        return seats

    @pytest.fixture
    def ba_tier(
        self,
        cart_event: Event,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
        ba_seats: list[VenueSeat],
    ) -> TicketTier:
        return TicketTier.objects.create(
            event=cart_event,
            name="BA Tier",
            price=Decimal("40.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            venue=venue,
            sector=sector,
            category_prices={str(category.id): "0"},
        )

    @pytest.fixture
    def none_tier(self, cart_event: Event) -> TicketTier:
        return TicketTier.objects.create(
            event=cart_event,
            name="GA Tier",
            price=Decimal("20.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )

    def test_ba_group_gets_block_none_group_gets_nones(
        self,
        cart_event: Event,
        ba_tier: TicketTier,
        none_tier: TicketTier,
        ba_seats: list[VenueSeat],
        category: PriceCategory,
        member_user: RevelUser,
    ) -> None:
        ba_group = CartGroup(tier=ba_tier, items=[_item(), _item()], price_category_id=category.id)
        none_group = CartGroup(tier=none_tier, items=[_item(), _item()])
        service = BatchTicketService(cart_event, user=member_user, groups=[ba_group, none_group])

        resolved = service.resolve_cart_seats([ba_group, none_group])

        assert {s.id for s in resolved[0] if s is not None} == {s.id for s in ba_seats}
        assert resolved[1] == [None, None]
