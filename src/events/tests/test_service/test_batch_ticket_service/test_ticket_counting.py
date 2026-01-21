"""Tests for BatchTicketService ticket counting methods."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    Ticket,
    TicketTier,
)
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


class TestGetUserTicketCount:
    """Tests for get_user_ticket_count method."""

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
    def tier(self, event: Event) -> TicketTier:
        """Create a test ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Test Tier",
            price=Decimal("25.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    def test_returns_zero_when_no_tickets(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return 0 when user has no tickets."""
        service = BatchTicketService(event, tier, member_user)
        assert service.get_user_ticket_count() == 0

    def test_counts_pending_tickets(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should count PENDING tickets."""
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=member_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Guest 1",
        )
        service = BatchTicketService(event, tier, member_user)
        assert service.get_user_ticket_count() == 1

    def test_counts_active_tickets(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should count ACTIVE tickets."""
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=member_user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Guest 1",
        )
        service = BatchTicketService(event, tier, member_user)
        assert service.get_user_ticket_count() == 1

    def test_excludes_cancelled_tickets(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should not count CANCELLED tickets."""
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=member_user,
            status=Ticket.TicketStatus.CANCELLED,
            guest_name="Guest 1",
        )
        service = BatchTicketService(event, tier, member_user)
        assert service.get_user_ticket_count() == 0

    def test_counts_only_same_tier(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should only count tickets for the same tier."""
        other_tier = TicketTier.objects.create(
            event=event,
            name="VIP",
            price=Decimal("100.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        Ticket.objects.create(
            event=event,
            tier=other_tier,
            user=member_user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Guest 1",
        )
        service = BatchTicketService(event, tier, member_user)
        assert service.get_user_ticket_count() == 0


class TestGetRemainingTickets:
    """Tests for get_remaining_tickets method."""

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
            max_tickets_per_user=3,
        )

    @pytest.fixture
    def tier(self, event: Event) -> TicketTier:
        """Create a test ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Test Tier",
            price=Decimal("25.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    def test_returns_max_when_no_tickets(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return full max when user has no tickets."""
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets() == 3

    def test_subtracts_existing_tickets(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should subtract existing tickets from max."""
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=member_user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Guest 1",
        )
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets() == 2

    def test_returns_none_when_unlimited(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return None when there's no limit."""
        event.max_tickets_per_user = None
        event.save()
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets() is None

    def test_respects_tier_override(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should use tier-level override when set."""
        tier.max_tickets_per_user = 10
        tier.save()
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets() == 10

    def test_returns_zero_not_negative(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return 0, not negative, when over limit."""
        # Create more tickets than the limit (possible via admin override)
        for i in range(5):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets() == 0

    def test_respects_event_capacity_parameter(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should limit by event capacity when passed as parameter."""
        # Per-user limit is 3, but event only has 2 spots left
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets(event_capacity_remaining=2) == 2

    def test_returns_min_of_user_and_event_limits(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return minimum of per-user and event capacity limits."""
        # Per-user: 3
        # Event: 10 remaining (passed as parameter)
        service = BatchTicketService(event, tier, member_user)
        # min(3, 10) = 3
        assert service.get_remaining_tickets(event_capacity_remaining=10) == 3

    def test_event_capacity_as_most_restrictive(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return event capacity when it's the most restrictive."""
        # Per-user: 3
        # Event: only 1 spot left
        service = BatchTicketService(event, tier, member_user)
        assert service.get_remaining_tickets(event_capacity_remaining=1) == 1

    def test_unlimited_when_all_limits_none(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should return None when all limits are unlimited."""
        event.max_tickets_per_user = None
        event.save()
        tier.max_tickets_per_user = None
        tier.save()

        service = BatchTicketService(event, tier, member_user)
        # No event_capacity_remaining passed = unlimited
        assert service.get_remaining_tickets() is None

    def test_does_not_include_tier_capacity(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should NOT include tier capacity - that's handled by _assert_tier_capacity.

        Tier capacity check returns 429 "sold out" error which is different from
        user limit check's 400 error. Keeping them separate preserves correct semantics.
        """
        # Tier is sold out but user hasn't bought any yet
        tier.total_quantity = 5
        tier.quantity_sold = 5  # All sold out
        tier.save()

        service = BatchTicketService(event, tier, member_user)
        # Per-user limit is 3, user has 0 tickets, so remaining = 3
        # Tier capacity is NOT factored in here
        assert service.get_remaining_tickets() == 3
