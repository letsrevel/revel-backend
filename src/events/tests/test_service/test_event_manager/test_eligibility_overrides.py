"""Tests for invitation override logic in eligibility checks."""

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
    Venue,
)
from events.service.event_manager import EligibilityService

pytestmark = pytest.mark.django_db


def test_invitation_overrides_max_attendees(
    public_user: RevelUser, member_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """An invited user should get access even if the event is full."""
    private_event.max_attendees = 1
    private_event.save()
    invitation.overrides_max_attendees = True
    invitation.save()

    # A different user takes the only spot
    general_tier = TicketTier.objects.create(event=private_event, name="General")
    Ticket.objects.create(guest_name="Test Guest", event=private_event, user=member_user, tier=general_tier)

    # The invited user should still be allowed
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_invitation_waives_questionnaire(
    public_user: RevelUser, private_event: Event, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """An invited user with an override should get access despite questionnaire requirements."""
    # This user has no submission, which would normally fail
    EventInvitation.objects.create(user=public_user, event=private_event, waives_questionnaire=True)

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_invitation_overrides_effective_capacity_with_venue(
    public_user: RevelUser, member_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """An invited user should get access even if venue capacity is reached."""
    # Create venue with small capacity
    venue = Venue.objects.create(
        organization=private_event.organization,
        name="Small Venue",
        capacity=1,
    )
    private_event.venue = venue
    private_event.max_attendees = 0  # Unlimited by max_attendees, but venue limits to 1
    private_event.save()
    invitation.overrides_max_attendees = True
    invitation.save()

    # A different user takes the only spot (via venue capacity)
    general_tier = TicketTier.objects.create(event=private_event, name="General")
    Ticket.objects.create(guest_name="Test Guest", event=private_event, user=member_user, tier=general_tier)

    # Refresh event to get venue prefetched
    private_event = Event.objects.select_related("venue").get(pk=private_event.pk)

    # The invited user with override should still be allowed
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True
