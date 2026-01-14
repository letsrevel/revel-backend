"""Tests for GET /events/{event_id}/attendees endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    OrganizationMember,
    Ticket,
)
from events.tasks import build_attendee_visibility_flags

pytestmark = pytest.mark.django_db


def test_get_event_attendees(
    nonmember_client: Client,
    member_client: Client,
    organization_owner_client: Client,
    nonmember_user: RevelUser,
    member_user: RevelUser,
    public_event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Test that the attendee list endpoint correctly respects user visibility preferences."""
    url = reverse("api:event_attendee_list", kwargs={"event_id": public_event.id})

    # --- Arrange ---

    # 1. Create attendees with different privacy preferences
    attendee_always = nonmember_user
    attendee_always.general_preferences.show_me_on_attendee_list = "always"
    attendee_always.general_preferences.save()

    attendee_never = member_user
    attendee_never.general_preferences.show_me_on_attendee_list = "never"
    attendee_never.general_preferences.save()

    attendee_members = revel_user_factory()
    attendee_members.general_preferences.show_me_on_attendee_list = "to_members"
    attendee_members.general_preferences.save()

    # 2. Make them attendees of the public event
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(guest_name="Test Guest", event=public_event, user=nonmember_user, tier=tier)
    Ticket.objects.create(guest_name="Test Guest", event=public_event, user=attendee_always, tier=tier)
    Ticket.objects.create(guest_name="Test Guest", event=public_event, user=attendee_never, tier=tier)
    Ticket.objects.create(guest_name="Test Guest", event=public_event, user=attendee_members, tier=tier)

    # 3. For 'to_members' visibility to work, the viewer and target must be members.
    # member_user is already a member via the member_client fixture.
    # Let's also make the attendee a member.
    OrganizationMember.objects.create(organization=public_event.organization, user=attendee_members)

    # 4. Manually run the task that builds the visibility flags.
    build_attendee_visibility_flags(str(public_event.id))

    # --- Act & Assert ---

    # Case 1: Viewer is a non-member
    response_nonmember = nonmember_client.get(url)
    assert response_nonmember.status_code == 200
    data_nonmember = response_nonmember.json()
    assert data_nonmember["count"] == 1
    # Only the user with 'always' preference is visible
    assert data_nonmember["results"][0]["first_name"] == attendee_always.first_name

    # Case 2: Viewer is a member
    response_member = member_client.get(url)
    assert response_member.status_code == 200
    data_member = response_member.json()
    assert data_member["count"] == 2
    visible_fnames = {user["first_name"] for user in data_member["results"]}
    # 'always' and 'to_members' should be visible
    assert visible_fnames == {attendee_always.first_name, attendee_members.first_name}

    # Case 3: Viewer is the organization owner
    response_owner = organization_owner_client.get(url)
    assert response_owner.status_code == 200
    data_owner = response_owner.json()
    # Owner can see everyone regardless of preferences
    assert data_owner["count"] == 3
    visible_fnames_owner = {user["first_name"] for user in data_owner["results"]}
    assert visible_fnames_owner == {
        attendee_always.first_name,
        attendee_never.first_name,
        attendee_members.first_name,
    }
