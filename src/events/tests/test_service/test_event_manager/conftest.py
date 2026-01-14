"""Shared fixtures for event_manager tests."""

import pytest

from events.models import Event, TicketTier


@pytest.fixture
def free_tier(public_event: Event) -> TicketTier:
    """Create a free ticket tier for convenience."""
    return TicketTier.objects.create(
        event=public_event,
        name="Free Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
