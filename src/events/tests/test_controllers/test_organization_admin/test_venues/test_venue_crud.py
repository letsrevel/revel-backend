"""Tests for venue CRUD endpoints."""

import uuid

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Organization, OrganizationStaff, Venue, VenueSeat, VenueSector

pytestmark = pytest.mark.django_db


class TestVenueManagement:
    """Tests for venue CRUD endpoints."""

    def test_list_venues_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can list venues."""
        Venue.objects.create(organization=organization, name="Theater One")
        Venue.objects.create(organization=organization, name="Theater Two")

        url = reverse("api:list_organization_venues", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_list_venues_by_staff(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff can list venues."""
        Venue.objects.create(organization=organization, name="Main Hall")

        url = reverse("api:list_organization_venues", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_list_venues_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Test that regular members cannot list venues."""
        url = reverse("api:list_organization_venues", kwargs={"slug": organization.slug})
        response = member_client.get(url)

        assert response.status_code == 403

    def test_create_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can create a venue."""
        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "New Venue", "description": "A great venue", "capacity": 500}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Venue"
        assert data["description"] == "A great venue"
        assert data["capacity"] == 500
        assert data["sectors"] == []
        assert Venue.objects.filter(organization=organization, name="New Venue").exists()

    def test_create_venue_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff with edit_organization permission can create venues."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "Staff Venue"}

        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201

    def test_create_venue_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without edit_organization permission cannot create venues."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "Forbidden Venue"}

        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403

    def test_create_venue_generates_slug(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that venue slug is auto-generated from name."""
        url = reverse("api:create_organization_venue", kwargs={"slug": organization.slug})
        payload = {"name": "Grand Ballroom"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["slug"] == "grand-ballroom"

    def test_get_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can get venue details with sectors."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        VenueSector.objects.create(venue=venue, name="Balcony")
        VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse("api:get_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Theater"
        assert len(data["sectors"]) == 2
        sector_names = {s["name"] for s in data["sectors"]}
        assert sector_names == {"Balcony", "Orchestra"}

    def test_get_venue_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that getting a non-existent venue returns 404."""
        url = reverse("api:get_organization_venue", kwargs={"slug": organization.slug, "venue_id": uuid.uuid4()})
        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_update_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can update a venue."""
        venue = Venue.objects.create(organization=organization, name="Old Name", capacity=100)

        url = reverse("api:update_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "New Name", "capacity": 200}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data["capacity"] == 200

        venue.refresh_from_db()
        assert venue.name == "New Name"
        assert venue.capacity == 200

    def test_update_venue_preserves_slug(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that updating venue name does not change slug."""
        venue = Venue.objects.create(organization=organization, name="Original")
        original_slug = venue.slug

        url = reverse("api:update_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Changed Name"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        venue.refresh_from_db()
        assert venue.slug == original_slug

    def test_delete_venue_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can delete a venue."""
        venue = Venue.objects.create(organization=organization, name="To Delete")
        sector = VenueSector.objects.create(venue=venue, name="Section A")
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse("api:delete_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not Venue.objects.filter(id=venue.id).exists()
        assert not VenueSector.objects.filter(id=sector.id).exists()

    def test_delete_venue_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without permission cannot delete venues."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        venue = Venue.objects.create(organization=organization, name="Protected")

        url = reverse("api:delete_organization_venue", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_staff_client.delete(url)

        assert response.status_code == 403
        assert Venue.objects.filter(id=venue.id).exists()
