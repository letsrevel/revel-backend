"""Shared fixtures for BatchTicketService tests."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService


@pytest.fixture
def batch_event(organization: Organization) -> Event:
    """Future-dated public event used across batch-service tests."""
    return Event.objects.create(
        organization=organization,
        name="Batch Test Event",
        slug="batch-test-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=5,
    )


@pytest.fixture
def batch_offline_tier(batch_event: Event) -> TicketTier:
    """Offline-payment tier on the batch test event."""
    return TicketTier.objects.create(
        event=batch_event,
        name="Offline Entry",
        price=Decimal("25.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=100,
    )


@pytest.fixture
def batch_user(member_user: RevelUser) -> RevelUser:
    """Alias for the buyer user in batch-service tests."""
    return member_user


@pytest.fixture
def run_checkout_offline(
    batch_event: Event,
    batch_offline_tier: TicketTier,
    batch_user: RevelUser,
) -> t.Callable[..., list[Ticket]]:
    """Run BatchTicketService.create_batch() against the offline tier with N guests."""

    def _run(count: int = 1) -> list[Ticket]:
        service = BatchTicketService(batch_event, batch_offline_tier, batch_user)
        items = [TicketPurchaseItem(guest_name=f"Guest {i + 1}") for i in range(count)]
        result = service.create_batch(items)
        assert isinstance(result, list)
        return result

    return _run
