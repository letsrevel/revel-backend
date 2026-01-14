"""Tests for BatchTicketService validation methods."""

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
)
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


class TestValidateBatchSize:
    """Tests for validate_batch_size method."""

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

    def test_passes_when_within_limit(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should not raise when within limit."""
        service = BatchTicketService(event, tier, member_user)
        service.validate_batch_size(3)  # Should not raise

    def test_raises_when_exceeds_limit(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should raise HttpError when exceeding limit."""
        service = BatchTicketService(event, tier, member_user)
        with pytest.raises(HttpError) as exc_info:
            service.validate_batch_size(4)
        assert exc_info.value.status_code == 400
        assert "can only purchase 3" in str(exc_info.value.message)

    def test_raises_at_max_message_when_at_limit(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should show 'reached maximum' message when at limit."""
        # Use up all tickets
        for i in range(3):
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=member_user,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=f"Guest {i}",
            )
        service = BatchTicketService(event, tier, member_user)
        with pytest.raises(HttpError) as exc_info:
            service.validate_batch_size(1)
        assert exc_info.value.status_code == 400
        assert "reached the maximum" in str(exc_info.value.message)

    def test_passes_when_unlimited(
        self,
        event: Event,
        tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Should not raise when unlimited tickets allowed."""
        event.max_tickets_per_user = None
        event.save()
        service = BatchTicketService(event, tier, member_user)
        service.validate_batch_size(100)  # Should not raise
