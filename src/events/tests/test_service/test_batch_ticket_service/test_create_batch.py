"""Tests for BatchTicketService create_batch method."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

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


class TestCreateBatch:
    """Tests for create_batch method."""

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
            max_tickets_per_user=5,
        )

    @pytest.fixture
    def free_tier(self, event: Event) -> TicketTier:
        """Create a free ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Free Entry",
            price=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

    @pytest.fixture
    def offline_tier(self, event: Event) -> TicketTier:
        """Create an offline ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Pay at Door",
            price=Decimal("25.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            total_quantity=100,
        )

    @pytest.fixture
    def online_tier(self, event: Event) -> TicketTier:
        """Create an online (Stripe) ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Online Purchase",
            price=Decimal("50.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
            total_quantity=100,
        )

    def test_free_checkout_creates_active_tickets(
        self,
        event: Event,
        free_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should create ACTIVE tickets for free tier."""
        service = BatchTicketService(event, free_tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
        ]
        result = service.create_batch(items)
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(t.status == Ticket.TicketStatus.ACTIVE for t in result)
        assert result[0].guest_name == "Guest 1"
        assert result[1].guest_name == "Guest 2"

    def test_offline_checkout_creates_pending_tickets(
        self,
        event: Event,
        offline_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should create PENDING tickets for offline tier."""
        service = BatchTicketService(event, offline_tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
        ]
        result = service.create_batch(items)
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(t.status == Ticket.TicketStatus.PENDING for t in result)

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_online_checkout_returns_stripe_url(
        self,
        mock_stripe: MagicMock,
        event: Event,
        online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return Stripe checkout URL for online tier."""
        mock_stripe.return_value = "https://checkout.stripe.com/test"
        service = BatchTicketService(event, online_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest 1")]
        result = service.create_batch(items)
        assert result == "https://checkout.stripe.com/test"
        mock_stripe.assert_called_once()

    def test_updates_quantity_sold(
        self,
        event: Event,
        free_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should increment quantity_sold on tier."""
        initial_sold = free_tier.quantity_sold
        service = BatchTicketService(event, free_tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
        ]
        service.create_batch(items)
        free_tier.refresh_from_db()
        assert free_tier.quantity_sold == initial_sold + 2

    def test_validates_batch_size(
        self,
        event: Event,
        free_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should raise when batch exceeds max_tickets_per_user."""
        event.max_tickets_per_user = 2
        event.save()
        service = BatchTicketService(event, free_tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(3)]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 400
        assert "can only purchase 2" in str(exc_info.value.message)

    def test_validates_tier_capacity(
        self,
        event: Event,
        free_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should raise when tier is sold out."""
        free_tier.quantity_sold = free_tier.total_quantity or 0
        free_tier.save()
        service = BatchTicketService(event, free_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest 1")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429
        assert "sold out" in str(exc_info.value.message)

    def test_raises_when_partial_tier_capacity(
        self,
        event: Event,
        free_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should raise when trying to buy more than available."""
        free_tier.total_quantity = 5
        free_tier.quantity_sold = 4
        free_tier.save()
        service = BatchTicketService(event, free_tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(3)]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 400
        assert "Only 1 ticket(s) remaining" in str(exc_info.value.message)

    def test_validates_event_capacity(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Should raise when event max_attendees is reached."""
        event = Event.objects.create(
            organization=organization,
            name="Limited Event",
            slug="limited-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=5,
            max_tickets_per_user=10,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 5 existing tickets (at capacity)
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Existing Guest {i}",
            )

        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 429
        assert "sold out" in str(exc_info.value.message).lower()

    def test_raises_when_partial_event_capacity(
        self,
        organization: Organization,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Should raise when trying to buy more than event has room for."""
        event = Event.objects.create(
            organization=organization,
            name="Limited Event",
            slug="limited-event-2",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=10,
            max_tickets_per_user=10,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 8 existing tickets for ANOTHER user (2 spots remaining at event level)
        for i in range(8):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=nonmember_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Existing Guest {i}",
            )

        service = BatchTicketService(event, tier, member_user)
        # Try to buy 5 when only 2 spots remain at event level
        items = [TicketPurchaseItem(guest_name=f"New Guest {i}") for i in range(5)]
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 400
        assert "Only 2 spot(s) remaining" in str(exc_info.value.message)

    def test_event_capacity_ignores_cancelled_tickets(
        self,
        organization: Organization,
        member_user: RevelUser,
    ) -> None:
        """Cancelled tickets should not count toward event capacity."""
        event = Event.objects.create(
            organization=organization,
            name="Limited Event",
            slug="limited-event-3",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=5,
            max_tickets_per_user=10,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Create 3 active + 5 cancelled tickets
        for i in range(3):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Active Guest {i}",
            )
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.CANCELLED,
                guest_name=f"Cancelled Guest {i}",
            )

        # Should be able to buy 2 more (3 active, 5 max = 2 remaining)
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name=f"New Guest {i}") for i in range(2)]
        # Should succeed without raising
        result = service.create_batch(items)
        assert len(result) == 2

    def test_unlimited_event_capacity(
        self,
        organization: Organization,
        member_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Events with max_attendees=0 should have no capacity limit."""
        event = Event.objects.create(
            organization=organization,
            name="Unlimited Event",
            slug="unlimited-event",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=0,  # Unlimited
            max_tickets_per_user=None,  # Unlimited per user
        )
        tier = TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )

        # Create many tickets (for nonmember_user to avoid per-user check)
        for i in range(100):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=nonmember_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )

        # Should still be able to buy more
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="New Guest")]
        result = service.create_batch(items)
        assert len(result) == 1


class TestCreateBatchWithVenue:
    """Tests for create_batch with venue/sector/seat assignment."""

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
            for i in range(1, 11)
        ]

    @pytest.fixture
    def tier_with_sector(self, event: Event, venue: Venue, sector: VenueSector) -> TicketTier:
        """Create a tier with venue and sector."""
        return TicketTier.objects.create(
            event=event,
            name="Seated",
            price=Decimal("50.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        )

    def test_assigns_venue_and_sector_to_tickets(
        self,
        event: Event,
        tier_with_sector: TicketTier,
        member_user: RevelUser,
        venue: Venue,
        sector: VenueSector,
        seats: list[VenueSeat],
    ) -> None:
        """Should assign venue, sector, and seat to tickets."""
        service = BatchTicketService(event, tier_with_sector, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1", seat_id=seats[0].id),
            TicketPurchaseItem(guest_name="Guest 2", seat_id=seats[1].id),
        ]
        tickets = service.create_batch(items)
        assert isinstance(tickets, list)
        assert len(tickets) == 2
        for ticket in tickets:
            assert ticket.venue == venue
            assert ticket.sector == sector
        assert tickets[0].seat == seats[0]
        assert tickets[1].seat == seats[1]


class TestCreateBatchPWYC:
    """Tests for create_batch with PWYC (Pay What You Can) tiers."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """Create a test event."""
        return Event.objects.create(
            organization=organization,
            name="Test Event",
            slug="test-event-pwyc",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            max_tickets_per_user=5,
        )

    @pytest.fixture
    def pwyc_offline_tier(self, event: Event) -> TicketTier:
        """Create a PWYC offline ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="PWYC Offline",
            price=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("5.00"),
            pwyc_max=Decimal("50.00"),
            total_quantity=100,
        )

    @pytest.fixture
    def pwyc_at_the_door_tier(self, event: Event) -> TicketTier:
        """Create a PWYC at-the-door ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="PWYC At Door",
            price=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("10.00"),
            pwyc_max=Decimal("100.00"),
            total_quantity=100,
        )

    @pytest.fixture
    def pwyc_online_tier(self, event: Event) -> TicketTier:
        """Create a PWYC online ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="PWYC Online",
            price=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("5.00"),
            pwyc_max=Decimal("50.00"),
            total_quantity=100,
        )

    def test_pwyc_offline_stores_price_paid(
        self,
        event: Event,
        pwyc_offline_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """PWYC offline checkout should store price_paid on tickets."""
        service = BatchTicketService(event, pwyc_offline_tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
        ]
        pwyc_amount = Decimal("25.00")

        result = service.create_batch(items, price_override=pwyc_amount)

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(t.status == Ticket.TicketStatus.PENDING for t in result)
        assert all(t.price_paid == pwyc_amount for t in result)

    def test_pwyc_at_the_door_stores_price_paid(
        self,
        event: Event,
        pwyc_at_the_door_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """PWYC at-the-door checkout should store price_paid on tickets."""
        service = BatchTicketService(event, pwyc_at_the_door_tier, member_user)
        items = [
            TicketPurchaseItem(guest_name="Guest 1"),
            TicketPurchaseItem(guest_name="Guest 2"),
            TicketPurchaseItem(guest_name="Guest 3"),
        ]
        pwyc_amount = Decimal("75.50")

        result = service.create_batch(items, price_override=pwyc_amount)

        assert isinstance(result, list)
        assert len(result) == 3
        assert all(t.status == Ticket.TicketStatus.PENDING for t in result)
        assert all(t.price_paid == pwyc_amount for t in result)

    def test_pwyc_offline_without_price_override_stores_none(
        self,
        event: Event,
        pwyc_offline_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """PWYC offline checkout without price_override should have price_paid=None."""
        service = BatchTicketService(event, pwyc_offline_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest 1")]

        result = service.create_batch(items)  # No price_override

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].price_paid is None

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_pwyc_online_does_not_store_price_paid(
        self,
        mock_stripe: MagicMock,
        event: Event,
        pwyc_online_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """PWYC online checkout should NOT store price_paid (handled by Stripe)."""
        mock_stripe.return_value = "https://checkout.stripe.com/test"
        service = BatchTicketService(event, pwyc_online_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Guest 1")]
        pwyc_amount = Decimal("30.00")

        result = service.create_batch(items, price_override=pwyc_amount)

        assert result == "https://checkout.stripe.com/test"
        # Verify price_override was passed to stripe
        mock_stripe.assert_called_once()
        call_kwargs = mock_stripe.call_args.kwargs
        assert call_kwargs["price_override"] == pwyc_amount
        # Verify ticket was created without price_paid (online uses Payment.amount)
        ticket = Ticket.objects.get(event=event, user=member_user)
        assert ticket.price_paid is None

    def test_pwyc_offline_price_persists_in_database(
        self,
        event: Event,
        pwyc_offline_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Price paid should be correctly persisted in database."""
        service = BatchTicketService(event, pwyc_offline_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Persistence Test")]
        pwyc_amount = Decimal("42.99")

        result = service.create_batch(items, price_override=pwyc_amount)

        assert isinstance(result, list)
        # Verify via fresh DB query
        ticket_from_db = Ticket.objects.get(pk=result[0].pk)
        assert ticket_from_db.price_paid == pwyc_amount
        assert ticket_from_db.guest_name == "Persistence Test"

    def test_fixed_price_offline_does_not_use_price_paid(
        self,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Fixed-price offline tiers should not have price_paid set."""
        fixed_tier = TicketTier.objects.create(
            event=event,
            name="Fixed Offline",
            price=Decimal("30.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            price_type=TicketTier.PriceType.FIXED,
            total_quantity=100,
        )
        service = BatchTicketService(event, fixed_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Fixed Price Guest")]

        result = service.create_batch(items)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].price_paid is None  # No price_override passed
