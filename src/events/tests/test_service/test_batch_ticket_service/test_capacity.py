"""Tests for capacity enforcement in BatchTicketService.

Tests effective_capacity (min of max_attendees and venue.capacity) and
sector capacity hard limits for GA tiers.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


class TestEffectiveCapacity:
    """Tests for effective_capacity (min of max_attendees and venue.capacity)."""

    def test_venue_capacity_limits_when_max_attendees_zero(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """When max_attendees=0 (unlimited), venue.capacity should be the limit."""
        venue = Venue.objects.create(
            organization=organization,
            name="Small Venue",
            capacity=5,
        )
        event = Event.objects.create(
            organization=organization,
            name="Venue Limited Event",
            slug="venue-limited",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=0,  # Unlimited by max_attendees
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 5 tickets to fill venue capacity
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )

        # Should be blocked by venue capacity
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429
        assert "sold out" in str(exc_info.value.message).lower()

    def test_min_of_max_attendees_and_venue_capacity(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Effective capacity should be min(max_attendees, venue.capacity)."""
        venue = Venue.objects.create(
            organization=organization,
            name="Big Venue",
            capacity=100,  # Larger than max_attendees
        )
        event = Event.objects.create(
            organization=organization,
            name="Limited Event",
            slug="limited-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=5,  # This should be the effective limit
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 5 tickets (at max_attendees capacity)
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )

        # Should be blocked (even though venue has capacity=100)
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429

    def test_venue_capacity_lower_than_max_attendees(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """When venue.capacity < max_attendees, venue.capacity should be the limit."""
        venue = Venue.objects.create(
            organization=organization,
            name="Small Venue",
            capacity=3,  # Smaller than max_attendees
        )
        event = Event.objects.create(
            organization=organization,
            name="Venue Limited Event",
            slug="venue-limited-2",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,  # Large limit
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 3 tickets to fill venue capacity
        for i in range(3):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )

        # Should be blocked by venue capacity (3 < 100)
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429

    def test_no_venue_uses_max_attendees(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Event without venue should use max_attendees only."""
        event = Event.objects.create(
            organization=organization,
            name="No Venue Event",
            slug="no-venue",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=5,
            max_tickets_per_user=10,
            venue=None,  # No venue
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 5 tickets (at capacity)
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )

        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429


class TestSectorCapacityHardLimit:
    """Tests for sector capacity enforcement (hard limit for GA tiers)."""

    def test_sector_capacity_enforced_for_ga_tier(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """GA tiers with sector should enforce sector.capacity as hard limit."""
        venue = Venue.objects.create(
            organization=organization,
            name="Venue",
            capacity=100,  # Large venue capacity
        )
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            capacity=5,  # Small sector capacity
        )
        event = Event.objects.create(
            organization=organization,
            name="Sector Limited Event",
            slug="sector-limited",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,  # Large event capacity
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Floor GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,  # GA tier
        )
        # Create 5 tickets in this sector (at sector capacity)
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
                sector=sector,
            )

        # Should be blocked by sector capacity (even though event has space)
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429
        assert "sector is full" in str(exc_info.value.message).lower()

    def test_sector_capacity_partial_availability(
        self,
        organization: Organization,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Should raise when trying to buy more than sector has room for."""
        venue = Venue.objects.create(
            organization=organization,
            name="Venue",
            capacity=100,
        )
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            capacity=10,
        )
        event = Event.objects.create(
            organization=organization,
            name="Sector Event",
            slug="sector-partial",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Floor GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )
        # Create 8 tickets for ANOTHER user (2 spots remaining in sector)
        for i in range(8):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=nonmember_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
                sector=sector,
            )

        # Try to buy 5 when only 2 spots remain in sector
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"New Guest {i}") for i in range(5)]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 400
        assert "Only 2 spot(s) remaining in this sector" in str(exc_info.value.message)

    def test_sector_capacity_not_enforced_for_seated_tier(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Seated tiers should not check sector.capacity (limited by seat count)."""
        venue = Venue.objects.create(
            organization=organization,
            name="Venue",
            capacity=100,
        )
        sector = VenueSector.objects.create(
            venue=venue,
            name="Reserved",
            capacity=2,  # Small sector capacity
        )
        # Create 5 seats (more than sector.capacity)
        seats = [VenueSeat.objects.create(sector=sector, label=f"A{i}", is_active=True) for i in range(5)]
        event = Event.objects.create(
            organization=organization,
            name="Seated Event",
            slug="seated-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Reserved",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=sector,
            venue=venue,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,  # Seated tier
        )

        # Should be able to buy 3 tickets (more than sector.capacity=2)
        # because seated tiers are limited by seat count, not sector.capacity
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"Guest {i}", seat_id=seats[i].id) for i in range(3)]
        result = service.create_batch(items)
        assert len(result) == 3

    def test_ga_tier_without_sector_skips_sector_check(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """GA tier without sector should not check sector capacity."""
        venue = Venue.objects.create(
            organization=organization,
            name="Venue",
            capacity=100,
        )
        event = Event.objects.create(
            organization=organization,
            name="No Sector Event",
            slug="no-sector",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA No Sector",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=None,  # No sector
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )

        # Should succeed (no sector to limit)
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest")]
        result = service.create_batch(items)
        assert len(result) == 1

    def test_sector_without_capacity_skips_check(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Sector without capacity set should not enforce limit."""
        venue = Venue.objects.create(
            organization=organization,
            name="Venue",
        )
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            capacity=None,  # No capacity limit
        )
        event = Event.objects.create(
            organization=organization,
            name="Unlimited Sector Event",
            slug="unlimited-sector",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=0,  # Unlimited
            max_tickets_per_user=None,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Floor GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )
        # Create many tickets
        for i in range(50):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
                sector=sector,
            )

        # Should still be able to buy more
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        result = service.create_batch(items)
        assert len(result) == 1

    def test_sector_capacity_ignores_cancelled_tickets(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Cancelled tickets should not count toward sector capacity."""
        venue = Venue.objects.create(
            organization=organization,
            name="Venue",
        )
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            capacity=5,
        )
        event = Event.objects.create(
            organization=organization,
            name="Sector Event",
            slug="sector-cancelled",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            max_tickets_per_user=10,
            venue=venue,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Floor GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )
        # Create 3 active + 5 cancelled tickets
        for i in range(3):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Active Guest {i}",
                sector=sector,
            )
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.CANCELLED,
                guest_name=f"Cancelled Guest {i}",
                sector=sector,
            )

        # Should be able to buy 2 more (3 active, 5 capacity = 2 remaining)
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"New Guest {i}") for i in range(2)]
        result = service.create_batch(items)
        assert len(result) == 2
