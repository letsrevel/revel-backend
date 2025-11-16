"""Tests for RSVP admin endpoints."""

import orjson
import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventRSVP, OrganizationStaff

pytestmark = pytest.mark.django_db


# ===== List RSVPs Tests =====


def test_list_rsvps_by_owner(organization_owner_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that an event owner can list RSVPs."""
    # Create some RSVPs
    rsvp1 = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    rsvp2 = EventRSVP.objects.create(event=event, user=user2, status=EventRSVP.RsvpStatus.NO)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    rsvp_ids = {item["id"] for item in data["results"]}
    assert str(rsvp1.id) in rsvp_ids
    assert str(rsvp2.id) in rsvp_ids


def test_list_rsvps_by_staff_with_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser
) -> None:
    """Test that staff with invite_to_event permission can list RSVPs."""
    # Create RSVP
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_staff_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rsvp.id)


def test_list_rsvps_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that staff without invite_to_event permission cannot list RSVPs."""
    # Remove the permission
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_staff_client.get(url)

    assert response.status_code == 403


def test_list_rsvps_unauthorized(member_client: Client, event: Event) -> None:
    """Test that non-staff cannot list RSVPs."""
    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = member_client.get(url)

    assert response.status_code == 403  # Event is visible but user lacks permission


def test_list_rsvps_with_search(organization_owner_client: Client, event: Event) -> None:
    """Test searching RSVPs by user email."""
    user1 = RevelUser.objects.create_user(
        username="alice", email="alice@example.com", password="pass", first_name="Alice"
    )
    user2 = RevelUser.objects.create_user(username="bob", email="bob@example.com", password="pass", first_name="Bob")

    rsvp1 = EventRSVP.objects.create(event=event, user=user1, status=EventRSVP.RsvpStatus.YES)
    EventRSVP.objects.create(event=event, user=user2, status=EventRSVP.RsvpStatus.NO)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"search": "alice"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rsvp1.id)


def test_list_rsvps_filter_by_status(organization_owner_client: Client, event: Event) -> None:
    """Test filtering RSVPs by status."""
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")
    user3 = RevelUser.objects.create_user(username="user3", email="user3@example.com", password="pass")

    rsvp_yes = EventRSVP.objects.create(event=event, user=user1, status=EventRSVP.RsvpStatus.YES)
    EventRSVP.objects.create(event=event, user=user2, status=EventRSVP.RsvpStatus.NO)
    EventRSVP.objects.create(event=event, user=user3, status=EventRSVP.RsvpStatus.MAYBE)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"status": "yes"})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rsvp_yes.id)


def test_list_rsvps_filter_by_user(organization_owner_client: Client, event: Event) -> None:
    """Test filtering RSVPs by user_id."""
    user1 = RevelUser.objects.create_user(username="user1", email="user1@example.com", password="pass")
    user2 = RevelUser.objects.create_user(username="user2", email="user2@example.com", password="pass")

    rsvp1 = EventRSVP.objects.create(event=event, user=user1, status=EventRSVP.RsvpStatus.YES)
    EventRSVP.objects.create(event=event, user=user2, status=EventRSVP.RsvpStatus.NO)

    url = reverse("api:list_rsvps", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"user_id": str(user1.id)})

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rsvp1.id)


# ===== Get RSVP Tests =====


def test_get_rsvp_by_owner(organization_owner_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that an event owner can get an RSVP."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:get_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(rsvp.id)
    assert data["status"] == "yes"
    assert data["user"]["first_name"] == member_user.first_name
    assert data["user"]["last_name"] == member_user.last_name


def test_get_rsvp_not_found(organization_owner_client: Client, event: Event) -> None:
    """Test getting a non-existent RSVP returns 404."""
    from uuid import uuid4

    fake_rsvp_id = uuid4()
    url = reverse("api:get_rsvp", kwargs={"event_id": event.pk, "rsvp_id": fake_rsvp_id})
    response = organization_owner_client.get(url)

    assert response.status_code == 404


# ===== Create RSVP Tests =====


def test_create_rsvp_by_owner(organization_owner_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that an event owner can create an RSVP on behalf of a user."""
    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(member_user.id), "status": "yes"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["user"]["first_name"] == member_user.first_name
    assert data["user"]["last_name"] == member_user.last_name
    assert data["status"] == "yes"

    # Verify in database
    rsvp = EventRSVP.objects.get(event=event, user=member_user)
    assert rsvp.status == EventRSVP.RsvpStatus.YES


def test_create_rsvp_by_staff_with_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser
) -> None:
    """Test that staff with invite_to_event permission can create RSVPs."""
    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(member_user.id), "status": "maybe"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "maybe"


def test_create_rsvp_by_staff_without_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser, staff_member: OrganizationStaff
) -> None:
    """Test that staff without invite_to_event permission cannot create RSVPs."""
    # Remove the permission
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(member_user.id), "status": "yes"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_create_rsvp_unauthorized(member_client: Client, event: Event) -> None:
    """Test that non-staff cannot create RSVPs."""
    user = RevelUser.objects.create_user(username="testuser", email="test@example.com", password="pass")
    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(user.id), "status": "yes"}

    response = member_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403  # Event is visible but user lacks permission


def test_create_rsvp_updates_existing(organization_owner_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that creating an RSVP for existing user updates the RSVP."""
    # Create existing RSVP
    EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.NO)

    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(member_user.id), "status": "yes"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "yes"

    # Verify only one RSVP exists (updated, not created new)
    assert EventRSVP.objects.filter(event=event, user=member_user).count() == 1
    rsvp = EventRSVP.objects.get(event=event, user=member_user)
    assert rsvp.status == EventRSVP.RsvpStatus.YES


def test_create_rsvp_nonexistent_user(organization_owner_client: Client, event: Event) -> None:
    """Test creating an RSVP for non-existent user returns 404."""
    from uuid import uuid4

    fake_user_id = uuid4()
    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(fake_user_id), "status": "yes"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


# ===== Update RSVP Tests =====


def test_update_rsvp_by_owner(organization_owner_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that an event owner can update an RSVP."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    payload = {"status": "no"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "no"

    # Verify in database
    rsvp.refresh_from_db()
    assert rsvp.status == EventRSVP.RsvpStatus.NO


def test_update_rsvp_by_staff_with_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser
) -> None:
    """Test that staff with invite_to_event permission can update RSVPs."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.MAYBE)

    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    payload = {"status": "yes"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "yes"


def test_update_rsvp_by_staff_without_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser, staff_member: OrganizationStaff
) -> None:
    """Test that staff without invite_to_event permission cannot update RSVPs."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    # Remove the permission
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    payload = {"status": "no"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_update_rsvp_unauthorized(member_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that non-staff cannot update RSVPs."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    payload = {"status": "no"}

    response = member_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_update_rsvp_not_found(organization_owner_client: Client, event: Event) -> None:
    """Test updating a non-existent RSVP returns 404."""
    from uuid import uuid4

    fake_rsvp_id = uuid4()
    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": fake_rsvp_id})
    payload = {"status": "no"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


# ===== Delete RSVP Tests =====


def test_delete_rsvp_by_owner(organization_owner_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that an event owner can delete an RSVP."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:delete_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not EventRSVP.objects.filter(pk=rsvp.pk).exists()


def test_delete_rsvp_by_staff_with_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser
) -> None:
    """Test that staff with invite_to_event permission can delete RSVPs."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:delete_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    response = organization_staff_client.delete(url)

    assert response.status_code == 204
    assert not EventRSVP.objects.filter(pk=rsvp.pk).exists()


def test_delete_rsvp_by_staff_without_permission(
    organization_staff_client: Client, event: Event, member_user: RevelUser, staff_member: OrganizationStaff
) -> None:
    """Test that staff without invite_to_event permission cannot delete RSVPs."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    # Remove the permission
    perms = staff_member.permissions
    perms["default"]["invite_to_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:delete_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    response = organization_staff_client.delete(url)

    assert response.status_code == 403


def test_delete_rsvp_unauthorized(member_client: Client, event: Event, member_user: RevelUser) -> None:
    """Test that non-staff cannot delete RSVPs."""
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:delete_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    response = member_client.delete(url)

    assert response.status_code == 403


def test_delete_rsvp_not_found(organization_owner_client: Client, event: Event) -> None:
    """Test deleting a non-existent RSVP returns 404."""
    from uuid import uuid4

    fake_rsvp_id = uuid4()
    url = reverse("api:delete_rsvp", kwargs={"event_id": event.pk, "rsvp_id": fake_rsvp_id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404
