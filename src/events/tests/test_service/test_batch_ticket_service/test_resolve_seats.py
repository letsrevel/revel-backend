"""Tests for BatchTicketService resolve_seats method."""

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


class TestResolveSeatsModeNone:
    """Tests for resolve_seats with NONE mode."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """Create a test event."""
        return Event.objects.create(
            organization=organization,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def tier(self, event: Event) -> TicketTier:
        """Create a tier with NONE seat mode."""
        return TicketTier.objects.create(
            event=event,
            name="Test Tier",
            price=Decimal("25.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
        )

    def test_returns_nones_for_each_item(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return list of Nones matching item count."""
        service = BatchTicketService(event, tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
        ]
        seats = service.resolve_seats(items)
        assert seats == [None, None]


class TestResolveSeatsRandomMode:
    """Tests for resolve_seats with RANDOM mode."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """Create a test event."""
        return Event.objects.create(
            organization=organization,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def venue(self, organization: Organization) -> Venue:
        """Create a test venue."""
        return Venue.objects.create(
            organization=organization,
            name="Test Venue",
            capacity=100,
        )

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        """Create a test sector."""
        return VenueSector.objects.create(
            venue=venue,
            name="Section A",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )

    @pytest.fixture
    def seats(self, sector: VenueSector) -> list[VenueSeat]:
        """Create test seats."""
        return [
            VenueSeat.objects.create(
                sector=sector,
                label=f"A{i}",
                row="A",
                number=i,
                position={"x": i * 10, "y": 10},
                is_active=True,
            )
            for i in range(1, 6)
        ]

    @pytest.fixture
    def tier(self, event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        """Create a tier with RANDOM seat mode."""
        return TicketTier.objects.create(
            event=event,
            name="Reserved Seating",
            price=Decimal("50.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.RANDOM,
            venue=venue,
            sector=sector,
        )

    def test_returns_random_seats(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
        seats: list[VenueSeat],
    ) -> None:
        """Should return random available seats."""
        service = BatchTicketService(event, tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
        ]
        resolved = service.resolve_seats(items)
        assert len(resolved) == 2
        assert all(s in seats for s in resolved if s is not None)

    def test_raises_when_not_enough_seats(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
        seats: list[VenueSeat],
    ) -> None:
        """Should raise HttpError when not enough seats available."""
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(10)]
        with pytest.raises(HttpError) as exc_info:
            service.resolve_seats(items)
        assert exc_info.value.status_code == 400
        assert "Not enough seats" in str(exc_info.value.message)


class TestResolveSeatsUserChoiceMode:
    """Tests for resolve_seats with USER_CHOICE mode."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """Create a test event."""
        return Event.objects.create(
            organization=organization,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def venue(self, organization: Organization) -> Venue:
        """Create a test venue."""
        return Venue.objects.create(
            organization=organization,
            name="Test Venue",
            capacity=100,
        )

    @pytest.fixture
    def sector(self, venue: Venue) -> VenueSector:
        """Create a test sector."""
        return VenueSector.objects.create(
            venue=venue,
            name="Section A",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )

    @pytest.fixture
    def seats(self, sector: VenueSector) -> list[VenueSeat]:
        """Create test seats."""
        return [
            VenueSeat.objects.create(
                sector=sector,
                label=f"A{i}",
                row="A",
                number=i,
                position={"x": i * 10, "y": 10},
                is_active=True,
            )
            for i in range(1, 6)
        ]

    @pytest.fixture
    def tier(self, event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        """Create a tier with USER_CHOICE seat mode."""
        return TicketTier.objects.create(
            event=event,
            name="Pick Your Seat",
            price=Decimal("75.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            venue=venue,
            sector=sector,
        )

    def test_returns_selected_seats(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
        seats: list[VenueSeat],
    ) -> None:
        """Should return the specifically selected seats."""
        service = BatchTicketService(event, tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1", seat_id=seats[0].id),
            TicketPurchaseItem(guest_name="Guest 2", seat_id=seats[1].id),
        ]
        resolved = service.resolve_seats(items)
        assert len(resolved) == 2
        assert resolved[0] == seats[0]
        assert resolved[1] == seats[1]

    def test_raises_when_seat_not_specified(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
        seats: list[VenueSeat],
    ) -> None:
        """Should raise HttpError when seat_id is not provided."""
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest 1")]
        with pytest.raises(HttpError) as exc_info:
            service.resolve_seats(items)
        assert exc_info.value.status_code == 400
        assert "Seat selection is required" in str(exc_info.value.message)

    def test_raises_when_seat_taken(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
        organization_owner_user: RevelUser,
        seats: list[VenueSeat],
    ) -> None:
        """Should raise HttpError when selected seat is already taken."""
        # Create a ticket for the seat
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Other Guest",
            seat=seats[0],
            sector=seats[0].sector,
            venue=seats[0].sector.venue,
        )
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest 1", seat_id=seats[0].id)]
        with pytest.raises(HttpError) as exc_info:
            service.resolve_seats(items)
        assert exc_info.value.status_code == 400
        assert "no longer available" in str(exc_info.value.message)
