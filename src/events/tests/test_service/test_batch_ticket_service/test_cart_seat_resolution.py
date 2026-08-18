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
from unittest.mock import patch

import psycopg
import pytest
from django.db import OperationalError, connection
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
    """A seat that exists (in the SAME venue) but belongs to a different sector than
    the requesting group's tier is refused. The shared lock query is scoped to the
    union of this cart's USER_CHOICE sectors, so ``sector_b`` — home to another
    tier's seats, not this cart's — is outside that union too: the row is never
    matched (never locked), and the per-group count-match check rejects it exactly
    as it would a nonexistent id.
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


class TestForeignEventSeat:
    """A seat_id from a completely unrelated organization/venue — not just the
    wrong sector within this cart's own venue, but another tenant's data entirely.

    Regression coverage for the cross-event lock-contention (griefing) vector: the
    shared lock query used to be scoped only by ``id__in``/``is_active``, so a
    client-supplied foreign seat_id got ``select_for_update()``-locked for the life
    of the request before being rejected. The query is now additionally scoped to
    ``sector_id__in`` the union of this cart's USER_CHOICE sectors, so a foreign
    sector's row is excluded from the query's own result set — Postgres never
    matches, and therefore never locks, that row at all.
    """

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="Sector")

    @pytest.fixture
    def tier(self, cart_event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        return _uc_tier(cart_event, venue, sector, "UC Tier")

    @pytest.fixture
    def foreign_seat(self, django_user_model: type[RevelUser]) -> VenueSeat:
        """A seat in a totally unrelated organization/venue/sector — outside this cart's reach."""
        owner = django_user_model.objects.create_user(
            username="foreign-owner", email="foreign-owner@example.com", password="pass"
        )
        foreign_org = Organization.objects.create(
            name="Foreign Org", slug="foreign-org", owner=owner, accept_membership_requests=True
        )
        foreign_venue = Venue.objects.create(organization=foreign_org, name="Foreign Venue", capacity=100)
        foreign_sector = VenueSector.objects.create(venue=foreign_venue, name="Foreign Sector")
        return _make_seats(foreign_sector, 1)[0]

    def test_foreign_sector_seat_rejected_and_never_locked(
        self,
        cart_event: Event,
        tier: TicketTier,
        foreign_seat: VenueSeat,
        member_user: RevelUser,
    ) -> None:
        group = CartGroup(tier=tier, items=[_item(foreign_seat)])
        service = BatchTicketService(cart_event, user=member_user, groups=[group])

        with CaptureQueriesContext(connection) as ctx, pytest.raises(HttpError) as exc_info:
            service.resolve_cart_seats([group])

        assert exc_info.value.status_code == 400
        assert "not in the correct sector" in str(exc_info.value.message)

        # Sector-scoped: the query text itself carries the sector filter, and
        # (per the docstring above) a foreign-sector row can never be part of what
        # it matches/locks — the security fix under test.
        lock_queries = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        assert len(lock_queries) == 1
        assert "sector_id" in lock_queries[0]


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

    def test_deadlock_becomes_retryable_409(
        self,
        cart_event: Event,
        ba_tier: TicketTier,
        category: PriceCategory,
        member_user: RevelUser,
    ) -> None:
        """A Postgres deadlock (SQLSTATE 40P01) must reach the buyer as a retryable 409, not a 500.

        Mixed UC+BA carts widened the BA deadlock window (final-review I4): the cart's
        USER_CHOICE lock is held while each BA group runs its own lock rounds.
        """
        ba_group = CartGroup(tier=ba_tier, items=[_item()], price_category_id=category.id)
        service = BatchTicketService(cart_event, user=member_user, groups=[ba_group])
        deadlock = OperationalError("deadlock detected")
        deadlock.__cause__ = psycopg.errors.DeadlockDetected()  # sqlstate 40P01

        with patch("events.service.seating.best_available.pick_best_available", side_effect=deadlock):
            with pytest.raises(HttpError) as exc_info:
                service.resolve_cart_seats([ba_group])

        assert exc_info.value.status_code == 409
        assert "please try again" in str(exc_info.value.message)

    def test_non_deadlock_operational_error_propagates(
        self,
        cart_event: Event,
        ba_tier: TicketTier,
        category: PriceCategory,
        member_user: RevelUser,
    ) -> None:
        """Only 40P01 is translated — any other OperationalError keeps its own (500) handling."""
        ba_group = CartGroup(tier=ba_tier, items=[_item()], price_category_id=category.id)
        service = BatchTicketService(cart_event, user=member_user, groups=[ba_group])
        other = OperationalError("connection lost")
        other.__cause__ = psycopg.errors.AdminShutdown()

        with patch("events.service.seating.best_available.pick_best_available", side_effect=other):
            with pytest.raises(OperationalError):
                service.resolve_cart_seats([ba_group])


class TestSharedPoolCrossGroupExclusion:
    """Groups of ONE cart drawing from a shared pool must never get the same seat.

    Within the cart's single transaction an earlier group's seats are not yet sold,
    not held, and our own row locks don't conflict with themselves — so only the
    ``cart_assigned`` exclusion keeps the groups apart (#893 review finding). Two
    tiers sharing a sector is supported config (see ``PriceCategory``'s docstring),
    so all three collision paths get a regression test.
    """

    @pytest.fixture(autouse=True)
    def _event_venue(self, cart_event: Event, venue: Venue) -> None:
        """The BEST_AVAILABLE picker's candidate loader requires event.venue_id."""
        cart_event.venue = venue
        cart_event.save(update_fields=["venue"])

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        return VenueSector.objects.create(venue=venue, name="Shared Pool Sector")

    @pytest.fixture
    def seats(self, sector: VenueSector) -> list[VenueSeat]:
        return _make_seats(sector, 4)

    def _ba_tier(self, event: Event, venue: Venue, sector: VenueSector, name: str) -> TicketTier:
        """Flat-priced best-available tier — empty map, whole-sector pool, no zone."""
        return TicketTier.objects.create(
            event=event,
            name=name,
            price=Decimal("40.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            venue=venue,
            sector=sector,
        )

    def test_two_ba_groups_same_pool_get_disjoint_blocks(
        self,
        cart_event: Event,
        venue: Venue,
        member_user: RevelUser,
    ) -> None:
        """Same sector, same (absent) zone: identical candidates would make both
        groups pick the same seat without the exclusion. A 3-seat row is used so
        the center seat wins *strictly* (centrality 0 vs 1.0 — outside the
        near-equal band the tiebreak RNG shuffles), making the pre-fix collision
        deterministic rather than a coin flip; one seat per group so contiguity
        scoring can't 409 the second group legitimately.
        """
        ba_sector = VenueSector.objects.create(venue=venue, name="BA Pool Sector")
        ba_seats = _make_seats(ba_sector, 3)
        tier_a = self._ba_tier(cart_event, venue, ba_sector, "Student")
        tier_b = self._ba_tier(cart_event, venue, ba_sector, "Regular")
        group_a = CartGroup(tier=tier_a, items=[_item()])
        group_b = CartGroup(tier=tier_b, items=[_item()])
        service = BatchTicketService(cart_event, user=member_user, groups=[group_a, group_b])

        resolved = service.resolve_cart_seats([group_a, group_b])

        ids_a = {s.id for s in resolved[0] if s is not None}
        ids_b = {s.id for s in resolved[1] if s is not None}
        assert len(ids_a) == 1 and len(ids_b) == 1
        assert ids_a.isdisjoint(ids_b)
        assert (ids_a | ids_b) <= {s.id for s in ba_seats}

    def test_same_seat_in_two_uc_groups_raises_400(
        self,
        cart_event: Event,
        venue: Venue,
        sector: VenueSector,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """Per-group duplicate detection can't see across groups — the cart-wide check must."""
        tier_a = _uc_tier(cart_event, venue, sector, "Tier A")
        tier_b = _uc_tier(cart_event, venue, sector, "Tier B")
        group_a = CartGroup(tier=tier_a, items=[_item(seats[0])])
        group_b = CartGroup(tier=tier_b, items=[_item(seats[0])])
        service = BatchTicketService(cart_event, user=member_user, groups=[group_a, group_b])

        with pytest.raises(HttpError) as exc_info:
            service.resolve_cart_seats([group_a, group_b])

        assert exc_info.value.status_code == 400
        assert "purchased twice" in str(exc_info.value.message)

    def test_ba_group_excludes_later_uc_groups_seats(
        self,
        cart_event: Event,
        venue: Venue,
        sector: VenueSector,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """USER_CHOICE seats are committed to their groups before any BA pick runs,
        even when the BA group is processed first. The UC group names every seat but
        the edge one, which central-preferring scoring would otherwise never leave
        for a 1-seat pick.
        """
        ba_tier = self._ba_tier(cart_event, venue, sector, "BA First")
        uc_tier = _uc_tier(cart_event, venue, sector, "UC Second")
        ba_group = CartGroup(tier=ba_tier, items=[_item()])
        uc_group = CartGroup(tier=uc_tier, items=[_item(seats[1]), _item(seats[2]), _item(seats[3])])
        service = BatchTicketService(cart_event, user=member_user, groups=[ba_group, uc_group])

        resolved = service.resolve_cart_seats([ba_group, uc_group])

        assert [s.id for s in resolved[0] if s is not None] == [seats[0].id]
        assert [s.id for s in resolved[1] if s is not None] == [s.id for s in seats[1:]]
