"""Tests for event direct invitation management endpoints."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, OrganizationStaff

pytestmark = pytest.mark.django_db


# --- Tests for Direct Invitations ---


def test_create_direct_invitations_for_existing_users(
    organization_owner_client: Client, event: Event, public_user: RevelUser, member_user: RevelUser
) -> None:
    """Test creating direct invitations for existing users."""
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    tier = event.ticket_tiers.first()
    assert tier is not None
    payload = {
        "emails": [public_user.email, member_user.email],
        "waives_questionnaire": True,
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["created_invitations"] == 2
    assert data["pending_invitations"] == 0
    assert data["total_invited"] == 2

    # Verify invitations were created
    from events.models import EventInvitation

    invitations = EventInvitation.objects.filter(event=event)
    assert invitations.count() == 2
    assert set(invitations.values_list("user__email", flat=True)) == {public_user.email, member_user.email}


def test_create_direct_invitations_for_non_existing_users(organization_owner_client: Client, event: Event) -> None:
    """Test creating direct invitations for non-existing users."""
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    tier = event.ticket_tiers.first()
    assert tier is not None
    payload = {
        "emails": ["nonexistent1@example.com", "nonexistent2@example.com"],
        "waives_purchase": True,
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["created_invitations"] == 0
    assert data["pending_invitations"] == 2
    assert data["total_invited"] == 2

    # Verify pending invitations were created
    from events.models import PendingEventInvitation

    pending_invitations = PendingEventInvitation.objects.filter(event=event)
    assert pending_invitations.count() == 2
    assert set(pending_invitations.values_list("email", flat=True)) == {
        "nonexistent1@example.com",
        "nonexistent2@example.com",
    }


def test_create_direct_invitations_mixed_users(
    organization_owner_client: Client, event: Event, public_user: RevelUser
) -> None:
    """Test creating direct invitations for both existing and non-existing users."""
    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    tier = event.ticket_tiers.first()
    assert tier is not None
    payload = {
        "emails": [public_user.email, "new@example.com"],
        "custom_message": "Welcome to our event!",
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["created_invitations"] == 1
    assert data["pending_invitations"] == 1
    assert data["total_invited"] == 2


def test_create_direct_invitations_requires_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that creating direct invitations requires the invite_to_event permission."""
    # Remove the invite_to_event permission
    tier = event.ticket_tiers.first()
    assert tier is not None
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_direct_invitations", kwargs={"event_id": event.pk})
    payload = {
        "emails": ["test@example.com"],
        "send_notification": False,
        "tier_id": str(tier.id),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 403


def test_pending_invitation_conversion_on_user_creation(event: Event) -> None:
    """Test that pending invitations are converted to real invitations when users sign up."""
    from events.models import EventInvitation, PendingEventInvitation

    # Create a pending invitation
    PendingEventInvitation.objects.create(
        event=event,
        email="newuser@example.com",
        waives_questionnaire=True,
        custom_message="Test message",
    )

    assert PendingEventInvitation.objects.count() == 1
    assert EventInvitation.objects.count() == 0

    # Create a user with the same email
    new_user = RevelUser.objects.create(
        username="newuser",
        email="newuser@example.com",
        first_name="New",
        last_name="User",
    )

    # Check that the pending invitation was converted
    assert PendingEventInvitation.objects.count() == 0
    assert EventInvitation.objects.count() == 1

    invitation = EventInvitation.objects.first()
    assert invitation is not None
    assert invitation.user == new_user
    assert invitation.event == event
    assert invitation.waives_questionnaire is True
    assert invitation.custom_message == "Test message"


def test_list_event_invitations(
    organization_owner_client: Client, event: Event, public_user: RevelUser, member_user: RevelUser
) -> None:
    """Test listing event invitations."""
    from events.models import EventInvitation

    # Create some invitations
    EventInvitation.objects.create(event=event, user=public_user, waives_questionnaire=True)
    EventInvitation.objects.create(event=event, user=member_user, custom_message="Welcome!")

    url = reverse("api:list_event_invitations", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["results"]) == 2


def test_list_pending_invitations(organization_owner_client: Client, event: Event) -> None:
    """Test listing pending invitations."""
    from events.models import PendingEventInvitation

    # Create some pending invitations
    PendingEventInvitation.objects.create(event=event, email="test1@example.com", waives_purchase=True)
    PendingEventInvitation.objects.create(event=event, email="test2@example.com", custom_message="Join us!")

    url = reverse("api:list_pending_invitations", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["results"]) == 2


def test_delete_event_invitation(organization_owner_client: Client, event: Event, public_user: RevelUser) -> None:
    """Test deleting an event invitation."""
    from events.models import EventInvitation

    # Create an invitation
    invitation = EventInvitation.objects.create(event=event, user=public_user)

    url = reverse(
        "api:delete_invitation",
        kwargs={
            "event_id": event.pk,
            "invitation_type": "registered",
            "invitation_id": invitation.pk,
        },
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not EventInvitation.objects.filter(pk=invitation.pk).exists()


def test_delete_pending_invitation(organization_owner_client: Client, event: Event) -> None:
    """Test deleting a pending invitation."""
    from events.models import PendingEventInvitation

    # Create a pending invitation
    invitation = PendingEventInvitation.objects.create(event=event, email="test@example.com")

    url = reverse(
        "api:delete_invitation",
        kwargs={
            "event_id": event.pk,
            "invitation_type": "pending",
            "invitation_id": invitation.pk,
        },
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not PendingEventInvitation.objects.filter(pk=invitation.pk).exists()


def test_delete_nonexistent_invitation(organization_owner_client: Client, event: Event) -> None:
    """Test deleting a non-existent invitation returns 404."""
    from uuid import uuid4

    fake_invitation_id = uuid4()
    url = reverse(
        "api:delete_invitation",
        kwargs={
            "event_id": event.pk,
            "invitation_type": "registered",
            "invitation_id": fake_invitation_id,
        },
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 404
