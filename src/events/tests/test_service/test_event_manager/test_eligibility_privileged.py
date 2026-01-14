"""Tests for privileged access (owner/staff) eligibility checks."""

import pytest

from accounts.models import RevelUser
from events.models import Event, EventInvitation, OrganizationStaff, TicketTier
from events.service.event_manager import EligibilityService

pytestmark = pytest.mark.django_db


def test_owner_gets_immediate_access(organization_owner_user: RevelUser, public_event: Event) -> None:
    """The organization owner should always get access with the 'staff' tier."""
    handler = EligibilityService(user=organization_owner_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_staff_gets_immediate_access(
    organization_staff_user: RevelUser, public_event: Event, staff_member: OrganizationStaff
) -> None:
    """A staff member should always get access with the 'staff' tier."""
    handler = EligibilityService(user=organization_staff_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_staff_tier_overrides_invitation_tier(
    organization_staff_user: RevelUser, private_event: Event, vip_tier: TicketTier, staff_member: OrganizationStaff
) -> None:
    """Tests logical hierarchy: staff access is checked before invitations."""
    # Invite a staff member to a VIP tier
    TicketTier.objects.create(event=private_event, name="VIP")  # ensure tier exists for private event
    EventInvitation.objects.create(user=organization_staff_user, event=private_event, tier=vip_tier)

    handler = EligibilityService(user=organization_staff_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True
