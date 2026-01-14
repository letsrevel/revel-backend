"""Tests for standard access eligibility and basic gate checks."""

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.service.event_manager import EligibilityService, NextStep, Reasons

pytestmark = pytest.mark.django_db


# --- Test Cases for Standard Access and Tiers ---


def test_public_user_gets_access_to_public_event(public_user: RevelUser, public_event: Event) -> None:
    """A general user should get access to a public event with no specific tier."""
    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_member_gets_member_tier_for_public_event(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember
) -> None:
    """A member should be assigned the 'member' tier for a public event."""
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_invited_user_gets_invited_tier(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """An invited user should be assigned the tier from their invitation."""
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Gates (Failures) ---


def test_event_is_full(public_user: RevelUser, member_user: RevelUser, public_event: Event) -> None:
    """Test the availability gate: deny access if event is at max capacity."""
    public_event.max_attendees = 1
    public_event.waitlist_open = True
    public_event.save()

    # The first user takes the only spot
    general_tier = TicketTier.objects.create(event=public_event, name="General")
    Ticket.objects.create(guest_name="Test Guest", event=public_event, user=public_user, tier=general_tier)

    # The second user should be denied access
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.EVENT_IS_FULL
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.JOIN_WAITLIST


def test_event_no_max_attendees(public_user: RevelUser, member_user: RevelUser, public_event: Event) -> None:
    """Test that if max_attendees are 0, the event is open to all."""
    public_event.max_attendees = 0
    public_event.save()
    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()
    assert eligibility.allowed


def test_private_event_requires_invitation(public_user: RevelUser, private_event: Event) -> None:
    """A non-invited user should be denied access to a private event."""
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.REQUEST_INVITATION


def test_private_event_pending_invitation_request(public_user: RevelUser, private_event: Event) -> None:
    """A user with a pending invitation request should wait for approval."""
    EventInvitationRequest.objects.create(
        event=private_event,
        user=public_user,
        status=EventInvitationRequest.InvitationRequestStatus.PENDING,
    )

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.INVITATION_REQUEST_PENDING
    assert eligibility.next_step == NextStep.WAIT_FOR_INVITATION_APPROVAL


def test_private_event_rejected_invitation_request(public_user: RevelUser, private_event: Event) -> None:
    """A user with a rejected invitation request should have no next step available."""
    EventInvitationRequest.objects.create(
        event=private_event,
        user=public_user,
        status=EventInvitationRequest.InvitationRequestStatus.REJECTED,
    )

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.INVITATION_REQUEST_REJECTED
    assert eligibility.next_step is None


def test_private_event_no_invitation_requests_accepted(public_user: RevelUser, private_event: Event) -> None:
    """When event doesn't accept invitation requests, next_step should be None."""
    private_event.accept_invitation_requests = False
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert eligibility.next_step is None


def test_members_only_event_requires_membership(public_user: RevelUser, members_only_event: Event) -> None:
    """A non-member should be denied access to a members-only event."""
    handler = EligibilityService(user=public_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERS_ONLY
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.BECOME_MEMBER


def test_members_only_event_blocks_inactive_member(member_user: RevelUser, members_only_event: Event) -> None:
    """A member with inactive status should be denied access to a members-only event."""
    # Create a membership with PAUSED status
    membership = OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=member_user,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_INACTIVE
    assert eligibility.next_step is None  # User needs to contact org to reactivate

    # Test with CANCELLED status
    membership.status = OrganizationMember.MembershipStatus.CANCELLED
    membership.save()

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_INACTIVE

    # Test with BANNED status
    membership.status = OrganizationMember.MembershipStatus.BANNED
    membership.save()

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_INACTIVE


def test_members_only_event_allows_active_member(member_user: RevelUser, members_only_event: Event) -> None:
    """An active member should be allowed access to a members-only event."""
    # Create a membership with ACTIVE status
    OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=member_user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True
