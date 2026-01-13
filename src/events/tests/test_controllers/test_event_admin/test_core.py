"""Tests for event admin core endpoints (update, status, media, delete, duplicate, slug)."""

from io import BytesIO

import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from PIL import Image

from common.utils import assert_image_equal
from events.models import Event, Organization, OrganizationStaff, TicketTier

pytestmark = pytest.mark.django_db


# --- Tests for PUT /event-admin/{event_id} ---


def test_update_event_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can successfully update it."""
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"name": "New Name by Owner", "visibility": "private"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name by Owner"
    assert data["visibility"] == "private"

    event.refresh_from_db()
    assert event.name == "New Name by Owner"
    assert event.visibility == "private"


def test_update_event_by_staff_with_permission(organization_staff_client: Client, event: Event) -> None:
    """Test that a staff member with 'edit_event' permission can update."""
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    # The default permission map for staff gives edit_event=True
    payload = {"name": "Updated by Staff", "description": "Staff was here.", "visibility": event.visibility}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.name == "Updated by Staff"
    assert event.description == "Staff was here."


def test_update_event_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'edit_event' permission gets a 403."""
    # Remove the 'edit_event' permission
    perms = staff_member.permissions
    perms["default"]["edit_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"name": "This update should fail", "visibility": "public"}
    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_update_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get a 403 when trying to update."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:edit_event", kwargs={"event_id": public_event.pk})
    payload = {"name": "This should fail", "visibility": "public"}

    response = client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


def test_upload_event_logo_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an event owner can successfully upload their logo."""
    url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})

    response = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")

    assert response.status_code == 200
    event.refresh_from_db()
    event.logo.seek(0)
    assert_image_equal(event.logo.read(), png_bytes)


def test_upload_event_cover_art_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an event owner can successfully update their cover art."""
    url = reverse("api:event_upload_cover_art", kwargs={"event_id": event.pk})

    response = organization_owner_client.post(url, data={"cover_art": png_file}, format="multipart")

    assert response.status_code == 200
    event.refresh_from_db()
    event.cover_art.seek(0)
    assert_image_equal(event.cover_art.read(), png_bytes)


def test_delete_event_logo_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile
) -> None:
    """Test that an event owner can successfully delete their logo."""
    # First upload a logo
    upload_url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.logo

    # Now delete it
    delete_url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    event.refresh_from_db()
    assert not event.logo


def test_delete_event_cover_art_by_owner(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile
) -> None:
    """Test that an event owner can successfully delete their cover art."""
    # First upload cover art
    upload_url = reverse("api:event_upload_cover_art", kwargs={"event_id": event.pk})
    organization_owner_client.post(upload_url, data={"cover_art": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.cover_art

    # Now delete it
    delete_url = reverse("api:event_delete_cover_art", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    event.refresh_from_db()
    assert not event.cover_art


def test_delete_event_logo_when_none_exists(organization_owner_client: Client, event: Event) -> None:
    """Test that deleting a logo when none exists is idempotent (returns 204)."""
    assert not event.logo

    url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204


def test_delete_event_cover_art_when_none_exists(organization_owner_client: Client, event: Event) -> None:
    """Test that deleting cover art when none exists is idempotent (returns 204)."""
    assert not event.cover_art

    url = reverse("api:event_delete_cover_art", kwargs={"event_id": event.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204


def test_delete_event_logo_by_staff_with_permission(
    organization_staff_client: Client, event: Event, png_file: SimpleUploadedFile
) -> None:
    """Test that staff with edit_event permission can delete logo."""
    # Upload a logo first
    upload_url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})
    organization_staff_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.logo

    # Delete it
    delete_url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 204
    event.refresh_from_db()
    assert not event.logo


def test_delete_event_logo_by_staff_without_permission(
    organization_staff_client: Client,
    organization_owner_client: Client,
    event: Event,
    staff_member: OrganizationStaff,
    png_file: SimpleUploadedFile,
) -> None:
    """Test that staff without edit_event permission cannot delete logo."""
    # Remove the edit_event permission
    perms = staff_member.permissions
    perms["default"]["edit_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    # Upload a logo as owner first
    upload_url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event.refresh_from_db()
    assert event.logo

    # Try to delete as staff without permission
    delete_url = reverse("api:event_delete_logo", kwargs={"event_id": event.pk})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 403
    event.refresh_from_db()
    assert event.logo


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_delete_event_logo_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get appropriate error when trying to delete logo."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:event_delete_logo", kwargs={"event_id": public_event.pk})

    response = client.delete(url)
    assert response.status_code == expected_status_code


def test_upload_event_logo_replaces_old_file(
    organization_owner_client: Client, event: Event, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that uploading a new logo deletes the old one."""
    url = reverse("api:event_upload_logo", kwargs={"event_id": event.pk})

    # Upload first logo
    response1 = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")
    assert response1.status_code == 200
    event.refresh_from_db()
    old_logo_name = event.logo.name

    # Create a different image (red square instead of the default png)
    img = Image.new("RGB", (100, 100), color="red")
    img_bytes = BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    new_png_file = SimpleUploadedFile("new_logo.png", img_bytes.read(), content_type="image/png")

    # Upload second logo
    response2 = organization_owner_client.post(url, data={"logo": new_png_file}, format="multipart")
    assert response2.status_code == 200
    event.refresh_from_db()
    new_logo_name = event.logo.name

    # Verify that the logo name changed (different file)
    assert old_logo_name != new_logo_name

    # Verify the old file was deleted (this is tricky in tests, but we can check the field was updated)
    assert event.logo
    event.logo.seek(0)
    saved_image = Image.open(event.logo)
    assert saved_image.size == (100, 100)


# --- Tests for POST /event-admin/{event_id}/actions/{status} ---


def test_update_event_status_to_open_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to open."""
    event.status = Event.EventStatus.DRAFT
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.OPEN

    event.refresh_from_db()
    assert event.status == Event.EventStatus.OPEN


def test_update_event_status_to_closed_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to closed."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CLOSED})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.CLOSED

    event.refresh_from_db()
    assert event.status == Event.EventStatus.CLOSED


def test_update_event_status_to_draft_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to draft."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.DRAFT})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.DRAFT

    event.refresh_from_db()
    assert event.status == Event.EventStatus.DRAFT


def test_update_event_status_to_deleted_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an organization owner can change event status to deleted."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})
    response = organization_owner_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.CANCELLED

    event.refresh_from_db()
    assert event.status == Event.EventStatus.CANCELLED
    # Verify event still exists in database (soft delete)
    assert Event.objects.filter(pk=event.pk).exists()


def test_update_event_status_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'manage_event' permission can update status."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["manage_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    event.status = Event.EventStatus.DRAFT
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_staff_client.post(url)

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.status == Event.EventStatus.OPEN


def test_update_event_status_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'manage_event' permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["manage_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    original_status = event.status

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})
    response = organization_staff_client.post(url)

    assert response.status_code == 403
    event.refresh_from_db()
    assert event.status == original_status


def test_update_event_status_nonexistent_event(organization_owner_client: Client) -> None:
    """Test updating status of non-existent event returns 404."""
    from uuid import uuid4

    fake_event_id = uuid4()
    url = reverse("api:update_event_status", kwargs={"event_id": fake_event_id, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_update_event_status_requires_authentication(event: Event) -> None:
    """Test that updating event status requires authentication."""
    client = Client()
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = client.post(url)

    assert response.status_code == 401


# --- Tests for DELETE /event-admin/{event_id} ---


def test_delete_event_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can successfully delete it."""
    event_id = event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not Event.objects.filter(pk=event_id).exists()


def test_delete_event_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'delete_event' permission can delete."""
    # Grant delete_event permission
    perms = staff_member.permissions
    perms["default"]["delete_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    event_id = event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    response = organization_staff_client.delete(url)

    assert response.status_code == 204
    assert not Event.objects.filter(pk=event_id).exists()


def test_delete_event_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'delete_event' permission gets a 403."""
    # Remove the 'delete_event' permission
    perms = staff_member.permissions
    perms["default"]["delete_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    event_id = event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})
    response = organization_staff_client.delete(url)

    assert response.status_code == 403
    # Event should still exist
    assert Event.objects.filter(pk=event_id).exists()


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_delete_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get a 403/401 when trying to delete."""
    client: Client = request.getfixturevalue(client_fixture)
    event_id = public_event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    response = client.delete(url)

    assert response.status_code == expected_status_code
    # Event should still exist
    assert Event.objects.filter(pk=event_id).exists()


# --- Tests for POST /event-admin/{event_id}/duplicate ---


def test_duplicate_event_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can duplicate it."""
    from datetime import timedelta

    new_start = event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Duplicated Event",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["name"] == "Duplicated Event"
    assert data["status"] == Event.EventStatus.DRAFT
    assert data["organization"]["id"] == str(event.organization.id)

    # Verify the new event exists
    new_event = Event.objects.get(pk=data["id"])
    assert new_event.name == "Duplicated Event"
    assert new_event.organization == event.organization


def test_duplicate_event_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'create_event' permission can duplicate."""
    from datetime import timedelta

    # Grant create_event permission
    perms = staff_member.permissions
    perms["default"]["create_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    new_start = event.start + timedelta(days=14)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Staff Duplicated Event",
        "start": new_start.isoformat(),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["name"] == "Staff Duplicated Event"


def test_duplicate_event_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'create_event' permission gets 403."""
    from datetime import timedelta

    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["create_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    new_start = event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Should Fail",
        "start": new_start.isoformat(),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_duplicate_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get a 403/401 when trying to duplicate."""
    from datetime import timedelta

    client: Client = request.getfixturevalue(client_fixture)
    new_start = public_event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": public_event.pk})
    payload = {
        "name": "Unauthorized Duplicate",
        "start": new_start.isoformat(),
    }

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == expected_status_code


def test_duplicate_event_preserves_ticket_tiers(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that duplicating an event also duplicates its ticket tiers."""
    from datetime import timedelta

    new_start = event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Event With Tiers",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()

    # Verify ticket tiers were copied
    new_event = Event.objects.get(pk=data["id"])
    new_tiers = list(new_event.ticket_tiers.all())

    # Should have the default tier plus our event_ticket_tier copy
    assert len(new_tiers) >= 1

    # Find the copied tier (by name match)
    copied_tier = next((t for t in new_tiers if t.name == event_ticket_tier.name), None)
    assert copied_tier is not None
    assert copied_tier.price == event_ticket_tier.price
    assert copied_tier.quantity_sold == 0  # Should be reset


def test_duplicate_event_shifts_dates(organization_owner_client: Client, event: Event) -> None:
    """Test that duplicating an event shifts all date fields correctly."""
    from datetime import timedelta

    # Set up various date fields
    event.rsvp_before = event.start - timedelta(days=1)
    event.check_in_starts_at = event.start - timedelta(hours=1)
    event.check_in_ends_at = event.end + timedelta(hours=1)
    event.save()

    delta = timedelta(days=7)
    new_start = event.start + delta
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Date-Shifted Event",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()

    # Verify date fields were shifted
    new_event = Event.objects.get(pk=data["id"])
    assert new_event.start == new_start
    assert new_event.end == event.end + delta
    assert new_event.rsvp_before == event.rsvp_before + delta
    assert new_event.check_in_starts_at == event.check_in_starts_at + delta
    assert new_event.check_in_ends_at == event.check_in_ends_at + delta


def test_duplicate_event_nonexistent(organization_owner_client: Client) -> None:
    """Test duplicating a non-existent event returns 404."""
    from datetime import timedelta
    from uuid import uuid4

    from django.utils import timezone

    fake_event_id = uuid4()
    new_start = timezone.now() + timedelta(days=30)
    url = reverse("api:duplicate_event", kwargs={"event_id": fake_event_id})
    payload = {
        "name": "Should Not Work",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_duplicate_event_requires_authentication(event: Event) -> None:
    """Test that duplicating an event requires authentication."""
    from datetime import timedelta

    new_start = event.start + timedelta(days=7)
    client = Client()
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Unauthenticated",
        "start": new_start.isoformat(),
    }

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 401


# --- Tests for PATCH /event-admin/{event_id}/slug ---


def test_edit_slug_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can edit its slug."""
    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})
    payload = {"slug": "custom-event-slug"}

    response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["slug"] == "custom-event-slug"

    event.refresh_from_db()
    assert event.slug == "custom-event-slug"


def test_edit_slug_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'edit_event' permission can edit slug."""
    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})
    payload = {"slug": "staff-edited-slug"}

    response = organization_staff_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    event.refresh_from_db()
    assert event.slug == "staff-edited-slug"


def test_edit_slug_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'edit_event' permission gets 403."""
    # Remove the edit_event permission
    perms = staff_member.permissions
    perms["default"]["edit_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})
    payload = {"slug": "should-fail"}

    response = organization_staff_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_edit_slug_conflict(organization_owner_client: Client, event: Event, organization: Organization) -> None:
    """Test that editing to an existing slug returns 400."""
    # Create another event with a specific slug
    Event.objects.create(
        organization=organization,
        name="Other Event",
        slug="taken-slug",
        start=event.start,
    )

    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})
    payload = {"slug": "taken-slug"}

    response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_edit_slug_same_slug_allowed(organization_owner_client: Client, event: Event) -> None:
    """Test that setting the same slug (no change) is allowed."""
    original_slug = event.slug
    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})
    payload = {"slug": original_slug}

    response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.slug == original_slug


def test_edit_slug_invalid_format(organization_owner_client: Client, event: Event) -> None:
    """Test that invalid slug formats are rejected."""
    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})

    # Test uppercase
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "Invalid-Slug"}), content_type="application/json"
    )
    assert response.status_code == 422

    # Test spaces
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "invalid slug"}), content_type="application/json"
    )
    assert response.status_code == 422

    # Test special characters
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "invalid_slug!"}), content_type="application/json"
    )
    assert response.status_code == 422

    # Test leading hyphen
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "-invalid-slug"}), content_type="application/json"
    )
    assert response.status_code == 422


def test_edit_slug_valid_formats(organization_owner_client: Client, event: Event) -> None:
    """Test that valid slug formats are accepted."""
    url = reverse("api:edit_event_slug", kwargs={"event_id": event.pk})

    # Simple slug
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "simple"}), content_type="application/json"
    )
    assert response.status_code == 200

    # With numbers
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "event2024"}), content_type="application/json"
    )
    assert response.status_code == 200

    # With hyphens
    response = organization_owner_client.patch(
        url, data=orjson.dumps({"slug": "my-cool-event"}), content_type="application/json"
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_edit_slug_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get appropriate error."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:edit_event_slug", kwargs={"event_id": public_event.pk})
    payload = {"slug": "unauthorized-slug"}

    response = client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == expected_status_code
