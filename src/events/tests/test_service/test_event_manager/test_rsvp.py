"""Tests for RSVP functionality and RSVP deadline gate."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Ticket,
    TicketTier,
)
from events.service.event_manager import EligibilityService, EventManager, Reasons, UserIsIneligibleError

pytestmark = pytest.mark.django_db


# --- Test Cases for RSVP ---


def test_private_event_rsvp_requires_invitation(public_user: RevelUser, private_event: Event) -> None:
    """Test that a user cannot RSVP without invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert not EventRSVP.objects.filter(event=private_event, user=public_user).exists()


def test_private_event_rsvp_requires_ticket(public_user: RevelUser, private_event: Event) -> None:
    """Test that a user cannot RSVP without invitation."""
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.REQUIRES_TICKET
    assert not EventRSVP.objects.filter(event=private_event, user=public_user).exists()


def test_private_event_rsvp_with_invitation(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """Test that a user can RSVP with an invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    handler.rsvp(EventRSVP.RsvpStatus.YES)

    rsvp = EventRSVP.objects.filter(event=private_event, user=public_user).first()
    assert rsvp is not None
    assert rsvp.status == EventRSVP.RsvpStatus.YES


def test_private_event_create_ticket_rsvp_only(
    public_user: RevelUser, private_event: Event, free_tier: TicketTier
) -> None:
    """Test that a user cannot RSVP without invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(free_tier)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.MUST_RSVP
    assert not Ticket.objects.filter(event=private_event, user=public_user).exists()


# --- Test Cases for RSVP Deadline Gate ---


def test_rsvp_deadline_passed_blocks_access(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline blocks access when deadline has passed."""
    # Set up event without tickets and with expired RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.RSVP_DEADLINE_PASSED
    assert eligibility.next_step is None


def test_rsvp_deadline_allows_access_before_deadline(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline allows access when deadline has not passed."""
    # Set up event without tickets and with future RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() + timedelta(hours=1)  # 1 hour from now
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_rsvp_deadline_ignored_for_ticket_events(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline is ignored for events that require tickets."""
    # Set up event with tickets and expired RSVP deadline
    public_event.requires_ticket = True
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since RSVP deadline doesn't apply to ticket events
    assert eligibility.allowed is True


def test_rsvp_deadline_waived_by_invitation(public_user: RevelUser, public_event: Event) -> None:
    """Test that invitation can waive RSVP deadline."""
    # Set up event without tickets and with expired RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    # Create invitation that waives RSVP deadline
    EventInvitation.objects.create(user=public_user, event=public_event, waives_rsvp_deadline=True)

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_rsvp_deadline_no_deadline_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that no RSVP deadline allows access."""
    # Set up event without tickets and no RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = None
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True
