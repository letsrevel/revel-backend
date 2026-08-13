"""Tests for aggregated sector capacity across cart groups (#846).

Two GA tiers can share a sector; ``assert_sector_capacities`` sums demand per
sector across the whole cart before locking-and-asserting, so two tiers can't
each individually pass while their sum oversells the sector.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier, Venue, VenueSector
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.batch_ticket_service.context import CartGroup

pytestmark = pytest.mark.django_db


@pytest.fixture
def sector_with_capacity_8(organization: Organization) -> VenueSector:
    """A venue sector with room for 8 tickets."""
    venue = Venue.objects.create(organization=organization, name="Aggregation Venue", capacity=100)
    return VenueSector.objects.create(venue=venue, name="Floor", capacity=8)


def make_ga_tier(event: Event, sector: VenueSector, name: str) -> TicketTier:
    """A free, unlimited GA tier pointing at ``sector``."""
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
    )


def make_tickets(event: Event, sector: VenueSector, user: RevelUser, count: int) -> None:
    """Create ``count`` ACTIVE tickets occupying ``sector``."""
    tier = make_ga_tier(event, sector, "Occupant tier")
    for i in range(count):
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=f"Occupant {i}",
            sector=sector,
        )


class TestSectorCapacityAggregation:
    """Two GA tiers sharing a sector must have their cart demand summed."""

    def test_two_ga_groups_sharing_sector_aggregate_oversell_raises_400(
        self,
        batch_event: Event,
        sector_with_capacity_8: VenueSector,
        batch_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """4 already sold (4 remaining); a 3+3 cart across two tiers oversells by 2."""
        tier_a = make_ga_tier(batch_event, sector_with_capacity_8, "Tier A")
        tier_b = make_ga_tier(batch_event, sector_with_capacity_8, "Tier B")
        make_tickets(batch_event, sector_with_capacity_8, nonmember_user, count=4)
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name=f"A{i}") for i in range(3)]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name=f"B{i}") for i in range(3)]),
        ]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)

        with pytest.raises(HttpError) as exc:
            service.assert_sector_capacities(groups)

        assert exc.value.status_code == 400
        assert "Only 4 spot(s) remaining in this sector" in str(exc.value.message)

    def test_two_ga_groups_sharing_sector_within_capacity_passes(
        self,
        batch_event: Event,
        sector_with_capacity_8: VenueSector,
        batch_user: RevelUser,
    ) -> None:
        """An empty sector with capacity 8 comfortably fits a 3+3 cart."""
        tier_a = make_ga_tier(batch_event, sector_with_capacity_8, "Tier A")
        tier_b = make_ga_tier(batch_event, sector_with_capacity_8, "Tier B")
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name=f"A{i}") for i in range(3)]),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem(guest_name=f"B{i}") for i in range(3)]),
        ]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)

        service.assert_sector_capacities(groups)  # must not raise

    def test_sold_out_sector_raises_429(
        self,
        batch_event: Event,
        sector_with_capacity_8: VenueSector,
        batch_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """A fully-occupied sector rejects even a single-ticket cart with 429."""
        tier_a = make_ga_tier(batch_event, sector_with_capacity_8, "Tier A")
        make_tickets(batch_event, sector_with_capacity_8, nonmember_user, count=8)
        groups = [CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="A0")])]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)

        with pytest.raises(HttpError) as exc:
            service.assert_sector_capacities(groups)

        assert exc.value.status_code == 429
        assert "sector is full" in str(exc.value.message).lower()

    def test_sector_rows_themselves_are_locked_before_counting(
        self,
        batch_event: Event,
        sector_with_capacity_8: VenueSector,
        batch_user: RevelUser,
    ) -> None:
        """The guard must lock the VenueSector row, not just the existing tickets.

        Locking the ticket rows cannot block a concurrent INSERT, so two carts on
        different tiers of the same sector would both pass and oversell. Pin that a
        ``SELECT ... FOR UPDATE`` against ``VenueSector`` is issued (a behavioural
        concurrency test is out of scope here — this pins the lock exists).
        """
        tier_a = make_ga_tier(batch_event, sector_with_capacity_8, "Tier A")
        groups = [CartGroup(tier=tier_a, items=[TicketPurchaseItem(guest_name="A0")])]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)

        with CaptureQueriesContext(connection) as captured:
            service.assert_sector_capacities(groups)

        sector_table = VenueSector._meta.db_table
        assert any("FOR UPDATE" in q["sql"].upper() and sector_table in q["sql"] for q in captured.captured_queries), [
            q["sql"] for q in captured.captured_queries
        ]

    def test_seated_and_no_sector_groups_are_skipped(
        self,
        batch_event: Event,
        sector_with_capacity_8: VenueSector,
        batch_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """A seated tier and a sector-less GA tier never query capacity, even next to a full sector."""
        # An unrelated sector that is completely full — must never be consulted.
        make_tickets(batch_event, sector_with_capacity_8, nonmember_user, count=8)

        seated_tier = TicketTier.objects.create(
            event=batch_event,
            name="Seated",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=sector_with_capacity_8,
            venue=sector_with_capacity_8.venue,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        )
        no_sector_tier = TicketTier.objects.create(
            event=batch_event,
            name="No Sector GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=None,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )
        groups = [
            CartGroup(tier=seated_tier, items=[TicketPurchaseItem(guest_name="S0")]),
            CartGroup(tier=no_sector_tier, items=[TicketPurchaseItem(guest_name="N0")]),
        ]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)

        service.assert_sector_capacities(groups)  # must not raise
