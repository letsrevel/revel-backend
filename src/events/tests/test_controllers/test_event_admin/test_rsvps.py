"""Tests for event RSVP management endpoints."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventRSVP,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
)

pytestmark = pytest.mark.django_db


# --- Tests for membership field in RSVP list endpoint ---


def test_list_rsvps_membership_null_for_non_member(
    organization_owner_client: Client,
    event: Event,
    nonmember_user: RevelUser,
    staff_member: OrganizationStaff,
) -> None:
    """Test that RSVP list returns membership=null for non-members."""
    # Grant invite permission to staff so we can access endpoint
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    # Create RSVP for non-member user
    rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1

    # Find our RSVP in the results
    rsvp_data = next((r for r in data["results"] if r["id"] == str(rsvp.id)), None)
    assert rsvp_data is not None
    assert rsvp_data["membership"] is None
    # Also verify user ID is present
    assert rsvp_data["user"]["id"] == str(nonmember_user.id)


def test_list_rsvps_membership_present_for_member(
    organization_owner_client: Client,
    organization: Organization,
    event: Event,
    nonmember_user: RevelUser,
) -> None:
    """Test that RSVP list returns membership object for organization members."""
    # Create membership tier and make user a member
    tier = MembershipTier.objects.create(organization=organization, name="Silver")
    membership = OrganizationMember.objects.create(organization=organization, user=nonmember_user, tier=tier)

    # Create RSVP for member user
    rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Find our RSVP in the results
    rsvp_data = next((r for r in data["results"] if r["id"] == str(rsvp.id)), None)
    assert rsvp_data is not None
    assert rsvp_data["membership"] is not None
    assert rsvp_data["membership"]["status"] == membership.status
    assert rsvp_data["membership"]["tier"]["name"] == tier.name
    # Also verify user ID is present
    assert rsvp_data["user"]["id"] == str(nonmember_user.id)
