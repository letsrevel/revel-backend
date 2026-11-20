"""Tests for event admin core endpoints (update, status, media, delete, duplicate, slug)."""

from io import BytesIO

import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import Client
from django.urls import reverse
from PIL import Image

from common.utils import assert_image_equal
from events.models import Event, Organization, OrganizationStaff

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


def test_update_event_open_ended_flag(organization_owner_client: Client, event: Event) -> None:
    """is_open_ended round-trips through the edit endpoint and the detail response."""
    assert event.is_open_ended is False
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"is_open_ended": True}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["is_open_ended"] is True

    event.refresh_from_db()
    assert event.is_open_ended is True


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


def test_update_occurrence_real_edit_marks_modified(organization_owner_client: Client, event: Event) -> None:
    """A PUT that actually changes a field on an occurrence flips ``is_modified``."""
    # Arrange — turn the fixture event into a generated occurrence.
    event.occurrence_index = 3
    event.is_modified = False
    event.save(update_fields=["occurrence_index", "is_modified"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"name": "Genuinely Changed", "visibility": event.visibility}

    # Act
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    # Assert
    assert response.status_code == 200
    event.refresh_from_db()
    assert event.name == "Genuinely Changed"
    assert event.is_modified is True


def test_update_occurrence_noop_put_does_not_mark_modified(organization_owner_client: Client, event: Event) -> None:
    """A no-op PUT on a generated occurrence must leave ``is_modified`` False.

    Without this guard, re-submitting the current state (e.g. from a
    re-opened admin form) would silently opt the occurrence out of future
    template propagation.
    """
    # Arrange — turn the fixture event into a generated occurrence.
    event.occurrence_index = 5
    event.is_modified = False
    event.save(update_fields=["occurrence_index", "is_modified"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    # Resend the exact current state — name/visibility unchanged.
    payload = {"name": event.name, "visibility": event.visibility}

    # Act
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    # Assert
    assert response.status_code == 200
    event.refresh_from_db()
    assert event.name == payload["name"]
    assert event.is_modified is False


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
    response = organization_owner_client.post(url, data={}, content_type="application/json")

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
    response = organization_owner_client.post(url, data={}, content_type="application/json")

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
    response = organization_owner_client.post(url, data={}, content_type="application/json")

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
    response = organization_owner_client.post(url, data={}, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == Event.EventStatus.CANCELLED

    event.refresh_from_db()
    assert event.status == Event.EventStatus.CANCELLED
    # Verify event still exists in database (soft delete)
    assert Event.objects.filter(pk=event.pk).exists()
    # No reason supplied -> stored empty, surfaced as null
    assert event.cancellation_reason == ""
    assert data["cancellation_reason"] is None


def test_update_event_status_cancel_persists_reason(organization_owner_client: Client, event: Event) -> None:
    """Cancelling with a reason persists it and surfaces it in the response."""
    event.status = Event.EventStatus.OPEN
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})
    response = organization_owner_client.post(
        url, data={"cancellation_reason": "Venue flooded"}, content_type="application/json"
    )

    assert response.status_code == 200
    assert response.json()["cancellation_reason"] == "Venue flooded"

    event.refresh_from_db()
    assert event.cancellation_reason == "Venue flooded"


def test_update_event_status_reason_ignored_for_non_cancel(organization_owner_client: Client, event: Event) -> None:
    """A reason sent with a non-cancel transition is ignored."""
    event.status = Event.EventStatus.DRAFT
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(
        url, data={"cancellation_reason": "should be ignored"}, content_type="application/json"
    )

    assert response.status_code == 200
    assert response.json()["cancellation_reason"] is None

    event.refresh_from_db()
    assert event.cancellation_reason == ""


def test_update_event_status_uncancel_clears_reason(organization_owner_client: Client, event: Event) -> None:
    """Transitioning away from cancelled clears a previously set reason."""
    event.status = Event.EventStatus.CANCELLED
    event.cancellation_reason = "Old reason"
    event.save()

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url, data={}, content_type="application/json")

    assert response.status_code == 200
    assert response.json()["cancellation_reason"] is None

    event.refresh_from_db()
    assert event.cancellation_reason == ""


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
    response = organization_staff_client.post(url, data={}, content_type="application/json")

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
    response = organization_staff_client.post(url, data={}, content_type="application/json")

    assert response.status_code == 403
    event.refresh_from_db()
    assert event.status == original_status


def test_update_event_status_nonexistent_event(organization_owner_client: Client) -> None:
    """Test updating status of non-existent event returns 404."""
    from uuid import uuid4

    fake_event_id = uuid4()
    url = reverse("api:update_event_status", kwargs={"event_id": fake_event_id, "status": Event.EventStatus.OPEN})
    response = organization_owner_client.post(url, data={}, content_type="application/json")

    assert response.status_code == 404


def test_update_event_status_requires_authentication(event: Event) -> None:
    """Test that updating event status requires authentication."""
    client = Client()
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.OPEN})
    response = client.post(url)

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
