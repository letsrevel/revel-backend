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
