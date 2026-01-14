"""Tests for organization admin resource endpoints (event series, events)."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from events.models import Organization, OrganizationStaff

pytestmark = pytest.mark.django_db


# --- Tests for Event Series ---


def test_create_event_series_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can create an event series."""
    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "New Event Series", "description": "A series of events."}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Event Series"
    assert data["organization"]["slug"] == organization.slug


def test_create_event_series_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'create_event_series' permission can create an event series."""
    perms = staff_member.permissions
    perms["default"]["create_event_series"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "Staff-Created Series", "description": "Created by staff."}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Staff-Created Series"


def test_create_event_series_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'create_event_series' permission gets a 403."""
    perms = staff_member.permissions
    perms["default"]["create_event_series"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "This should not be created", "description": "This should fail."}
    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code",
    [("member_client", 403), ("nonmember_client", 404), ("client", 401)],
)
def test_create_event_series_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
) -> None:
    """Test that unauthorized users get a 403 or 401 when trying to create an event series."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:create_event_series", kwargs={"slug": organization.slug})
    payload = {"name": "Unauthorized Series", "description": "This should fail."}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code


# --- Tests for Events ---


def test_create_event_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can create an event."""
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "New Event",
        "description": "A single event.",
        "visibility": "public",
        "status": "open",
        "event_type": "public",
        "waitlist_open": False,
        # "requires_ticket": False,  we test default behavior
        "address": "123 Main St",
        "start": timezone.now().timestamp(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Event"
    assert data["organization"]["slug"] == organization.slug
    assert data["requires_ticket"] is False


def test_create_event_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'create_event' permission can create an event."""
    perms = staff_member.permissions
    perms["default"]["create_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "Staff-Created Event",
        "description": "Created by staff.",
        "visibility": "public",
        "status": "open",
        "event_type": "public",
        "waitlist_open": False,
        "requires_ticket": False,
        "address": "123 Main St",
        "start": timezone.now().timestamp(),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Staff-Created Event"


def test_create_event_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'create_event' permission gets a 403."""
    perms = staff_member.permissions
    perms["default"]["create_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "This should not be created",
        "description": "This should fail.",
        "start": timezone.now().timestamp(),
    }
    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code",
    [("member_client", 403), ("nonmember_client", 404), ("client", 401)],
)
def test_create_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, organization: Organization
) -> None:
    """Test that unauthorized users get a 403 or 401 when trying to create an event."""
    client: Client = request.getfixturevalue(client_fixture)
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {"name": "Unauthorized Event", "description": "This should fail.", "start": timezone.now().timestamp()}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == expected_status_code
