import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from common.utils import assert_image_equal
from events.models import EventSeries, OrganizationStaff

pytestmark = pytest.mark.django_db


def test_update_event_series_by_owner(organization_owner_client: Client, event_series: EventSeries) -> None:
    """Test that an event series' organization owner can successfully update it."""
    url = reverse("api:edit_event_series", kwargs={"series_id": event_series.pk})
    payload = {"name": "New Name by Owner"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name by Owner"

    event_series.refresh_from_db()
    assert event_series.name == "New Name by Owner"


def test_update_event_series_by_staff_with_permission(
    organization_staff_client: Client, event_series: EventSeries, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'edit_event_series' permission can update."""
    perms = staff_member.permissions
    perms["default"]["edit_event_series"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_event_series", kwargs={"series_id": event_series.pk})
    payload = {"name": "Updated by Staff", "description": "Staff was here."}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event_series.refresh_from_db()
    assert event_series.name == "Updated by Staff"
    assert event_series.description == "Staff was here."


def test_update_event_series_by_staff_without_permission(
    organization_staff_client: Client, event_series: EventSeries, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'edit_event_series' permission gets a 403."""
    perms = staff_member.permissions
    perms["default"]["edit_event_series"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:edit_event_series", kwargs={"series_id": event_series.pk})
    payload = {"name": "This update should fail"}
    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_delete_event_series_by_owner(organization_owner_client: Client, event_series: EventSeries) -> None:
    """Test that an event series' organization owner can successfully delete it."""
    url = reverse("api:delete_event_series", kwargs={"series_id": event_series.pk})
    response = organization_owner_client.delete(url)
    assert response.status_code == 204
    assert not EventSeries.objects.filter(pk=event_series.pk).exists()


def test_delete_event_series_by_staff_with_permission(
    organization_staff_client: Client, event_series: EventSeries, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'delete_event_series' permission can delete."""
    perms = staff_member.permissions
    perms["default"]["delete_event_series"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:delete_event_series", kwargs={"series_id": event_series.pk})
    response = organization_staff_client.delete(url)
    assert response.status_code == 204
    assert not EventSeries.objects.filter(pk=event_series.pk).exists()


def test_delete_event_series_by_staff_without_permission(
    organization_staff_client: Client, event_series: EventSeries, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'delete_event_series' permission gets 403."""
    perms = staff_member.permissions
    perms["default"]["delete_event_series"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:delete_event_series", kwargs={"series_id": event_series.pk})
    response = organization_staff_client.delete(url)
    assert response.status_code == 403
    assert EventSeries.objects.filter(pk=event_series.pk).exists()


def test_upload_event_series_logo_by_owner(
    organization_owner_client: Client, event_series: EventSeries, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an event series owner can successfully upload their logo."""
    url = reverse("api:event_series_upload_logo", kwargs={"series_id": event_series.pk})

    response = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")

    assert response.status_code == 200
    event_series.refresh_from_db()
    event_series.logo.seek(0)
    assert_image_equal(event_series.logo.read(), png_bytes)


def test_upload_event_series_cover_art_by_owner(
    organization_owner_client: Client, event_series: EventSeries, png_file: SimpleUploadedFile, png_bytes: bytes
) -> None:
    """Test that an event series owner can successfully update their cover art."""
    url = reverse("api:event_series_upload_cover_art", kwargs={"series_id": event_series.pk})

    response = organization_owner_client.post(url, data={"cover_art": png_file}, format="multipart")

    assert response.status_code == 200
    event_series.refresh_from_db()
    event_series.cover_art.seek(0)
    assert_image_equal(event_series.cover_art.read(), png_bytes)


def test_delete_event_series_logo_by_owner(
    organization_owner_client: Client, event_series: EventSeries, png_file: SimpleUploadedFile
) -> None:
    """Test that an event series owner can successfully delete their logo."""
    # First upload a logo
    upload_url = reverse("api:event_series_upload_logo", kwargs={"series_id": event_series.pk})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event_series.refresh_from_db()
    assert event_series.logo

    # Now delete it
    delete_url = reverse("api:event_series_delete_logo", kwargs={"series_id": event_series.pk})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    event_series.refresh_from_db()
    assert not event_series.logo


def test_delete_event_series_cover_art_by_owner(
    organization_owner_client: Client, event_series: EventSeries, png_file: SimpleUploadedFile
) -> None:
    """Test that an event series owner can successfully delete their cover art."""
    # First upload cover art
    upload_url = reverse("api:event_series_upload_cover_art", kwargs={"series_id": event_series.pk})
    organization_owner_client.post(upload_url, data={"cover_art": png_file}, format="multipart")
    event_series.refresh_from_db()
    assert event_series.cover_art

    # Now delete it
    delete_url = reverse("api:event_series_delete_cover_art", kwargs={"series_id": event_series.pk})
    response = organization_owner_client.delete(delete_url)

    assert response.status_code == 204
    event_series.refresh_from_db()
    assert not event_series.cover_art


def test_delete_event_series_logo_when_none_exists(
    organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that deleting a logo when none exists is idempotent (returns 204)."""
    assert not event_series.logo

    url = reverse("api:event_series_delete_logo", kwargs={"series_id": event_series.pk})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204


def test_delete_event_series_logo_by_staff_with_permission(
    organization_staff_client: Client,
    event_series: EventSeries,
    staff_member: OrganizationStaff,
    png_file: SimpleUploadedFile,
) -> None:
    """Test that staff with edit_event_series permission can delete logo."""
    # Grant permission
    perms = staff_member.permissions
    perms["default"]["edit_event_series"] = True
    staff_member.permissions = perms
    staff_member.save()

    # Upload a logo first
    upload_url = reverse("api:event_series_upload_logo", kwargs={"series_id": event_series.pk})
    organization_staff_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event_series.refresh_from_db()
    assert event_series.logo

    # Delete it
    delete_url = reverse("api:event_series_delete_logo", kwargs={"series_id": event_series.pk})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 204
    event_series.refresh_from_db()
    assert not event_series.logo


def test_delete_event_series_logo_by_staff_without_permission(
    organization_staff_client: Client,
    organization_owner_client: Client,
    event_series: EventSeries,
    staff_member: OrganizationStaff,
    png_file: SimpleUploadedFile,
) -> None:
    """Test that staff without edit_event_series permission cannot delete logo."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["edit_event_series"] = False
    staff_member.permissions = perms
    staff_member.save()

    # Upload a logo as owner first
    upload_url = reverse("api:event_series_upload_logo", kwargs={"series_id": event_series.pk})
    organization_owner_client.post(upload_url, data={"logo": png_file}, format="multipart")
    event_series.refresh_from_db()
    assert event_series.logo

    # Try to delete as staff without permission
    delete_url = reverse("api:event_series_delete_logo", kwargs={"series_id": event_series.pk})
    response = organization_staff_client.delete(delete_url)

    assert response.status_code == 403
    event_series.refresh_from_db()
    assert event_series.logo


def test_upload_event_series_logo_replaces_old_file(
    organization_owner_client: Client, event_series: EventSeries, png_file: SimpleUploadedFile
) -> None:
    """Test that uploading a new logo deletes the old one."""
    url = reverse("api:event_series_upload_logo", kwargs={"series_id": event_series.pk})

    # Upload first logo
    response1 = organization_owner_client.post(url, data={"logo": png_file}, format="multipart")
    assert response1.status_code == 200
    event_series.refresh_from_db()
    old_logo_name = event_series.logo.name

    # Upload second logo
    from io import BytesIO

    from PIL import Image

    # Create a different image (green square)
    img = Image.new("RGB", (200, 200), color="green")
    img_bytes = BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    new_png_file = SimpleUploadedFile("new_logo.png", img_bytes.read(), content_type="image/png")

    response2 = organization_owner_client.post(url, data={"logo": new_png_file}, format="multipart")
    assert response2.status_code == 200
    event_series.refresh_from_db()
    new_logo_name = event_series.logo.name

    # Verify that the logo name changed (different file)
    assert old_logo_name != new_logo_name

    # Verify the new file is saved correctly
    assert event_series.logo
    event_series.logo.seek(0)
    saved_image = Image.open(event_series.logo)
    assert saved_image.size == (200, 200)
