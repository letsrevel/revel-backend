"""Tests for ticket sales window gate."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.service.event_manager import EligibilityService, Reasons

pytestmark = pytest.mark.django_db


def test_ticket_sales_window_active(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets can be purchased when sales window is active."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()

    # Create ticket tier with active sales window
    TicketTier.objects.create(
        event=public_event,
        name="General",
        sales_start_at=timezone.now() - timedelta(hours=1),  # Started 1 hour ago
        sales_end_at=timezone.now() + timedelta(hours=1),  # Ends in 1 hour
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_ticket_sales_window_not_started(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets cannot be purchased before sales window starts."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()
    public_event.ticket_tiers.all().delete()

    # Create ticket tier with future sales window
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() + timedelta(hours=1),  # Starts in 1 hour
        sales_end_at=timezone.now() + timedelta(hours=24),  # Ends in 24 hours
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE
    assert eligibility.next_step is None


def test_ticket_sales_window_ended(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets cannot be purchased after sales window ends."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()
    public_event.ticket_tiers.all().delete()

    # Create ticket tier with past sales window
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=24),  # Started 24 hours ago
        sales_end_at=timezone.now() - timedelta(hours=1),  # Ended 1 hour ago
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE
    assert eligibility.next_step is None


def test_ticket_sales_no_window_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets can be purchased when no sales window is set."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()

    # Create ticket tier with no sales window
    TicketTier.objects.create(
        event=public_event,
        name="General",
        # No sales_start_at or sales_end_at set
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_ticket_sales_multiple_tiers_one_active(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets can be purchased when at least one tier has active sales."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()

    # Create one tier with past sales window
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=24),
        sales_end_at=timezone.now() - timedelta(hours=1),
    )

    # Create another tier with active sales window
    TicketTier.objects.create(
        event=public_event,
        name="Regular",
        sales_start_at=timezone.now() - timedelta(hours=1),
        sales_end_at=timezone.now() + timedelta(hours=1),
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_ticket_sales_window_ignored_for_rsvp_events(public_user: RevelUser, public_event: Event) -> None:
    """Test that sales windows are ignored for events that don't require tickets."""
    # Set up event without tickets required
    public_event.requires_ticket = False
    public_event.save()

    # Create ticket tier with past sales window (should be ignored)
    TicketTier.objects.create(
        event=public_event,
        name="RSVP",
        sales_start_at=timezone.now() - timedelta(hours=24),
        sales_end_at=timezone.now() - timedelta(hours=1),
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since this is an RSVP event, not a ticket event
    assert eligibility.allowed is True


def test_ticket_sales_uses_event_end_when_sales_end_not_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that TicketSalesGate uses event end when sales_end_at is not provided."""
    # Set up event with tickets required and end time in the future
    public_event.requires_ticket = True
    public_event.start = timezone.now()
    public_event.end = timezone.now() + timedelta(hours=2)
    public_event.save()

    # Create ticket tier with sales_start_at but no sales_end_at
    TicketTier.objects.create(
        event=public_event,
        name="General",
        sales_start_at=timezone.now() - timedelta(hours=1),  # Started 1 hour ago
        sales_end_at=None,  # No explicit end time - should use event end
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since event hasn't ended yet
    assert eligibility.allowed is True


def test_ticket_sales_blocks_when_event_ended_and_no_sales_end(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets are blocked when event has ended and no sales_end_at is set."""
    # Set up event with tickets required and end time in the past
    public_event.requires_ticket = True
    public_event.start = timezone.now() - timedelta(hours=12)
    public_event.end = timezone.now() + timedelta(hours=1)
    public_event.save()

    public_event.ticket_tiers.all().delete()

    # Create ticket tier with no sales_end_at (should use past event end)
    TicketTier.objects.create(
        event=public_event,
        name="General",
        sales_start_at=public_event.start - timedelta(days=4),  # Started 4 hours ago
        sales_end_at=None,  # No explicit end time - should use event end
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be blocked since event has ended
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE


def test_ticket_sales_explicit_end_overrides_event_end(public_user: RevelUser, public_event: Event) -> None:
    """Test that explicit sales_end_at takes precedence over event end."""
    # Set up event with end time in the future
    public_event.ticket_tiers.all().delete()
    public_event.requires_ticket = True
    public_event.start = timezone.now()
    public_event.end = timezone.now() + timedelta(hours=5)
    public_event.save()
    public_event.ticket_tiers.all().delete()

    # Create ticket tier with explicit sales_end_at that is in the past
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=2),
        sales_end_at=timezone.now() - timedelta(hours=1),  # Explicit end in the past
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be blocked because explicit sales_end_at has passed, even though event hasn't ended
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE


def test_ticket_sales_mixed_tiers_one_uses_event_end(public_user: RevelUser, public_event: Event) -> None:
    """Test mixed scenario with one tier using event end and another with explicit end."""
    # Set up event with end time in the future
    public_event.requires_ticket = True
    public_event.start = timezone.now()
    public_event.end = timezone.now() + timedelta(hours=3)
    public_event.save()

    # Create one tier with explicit sales_end_at in the past
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=2),
        sales_end_at=timezone.now() - timedelta(hours=1),  # Ended 1 hour ago
    )

    # Create another tier with no sales_end_at (should use event end)
    TicketTier.objects.create(
        event=public_event,
        name="Regular",
        sales_start_at=timezone.now() - timedelta(hours=1),
        sales_end_at=None,  # Should use event end (3 hours from now)
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed because Regular tier is still on sale (using event end)
    assert eligibility.allowed is True
