"""Tests for venue sector CRUD endpoints."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Organization, Venue, VenueSeat, VenueSector

pytestmark = pytest.mark.django_db


class TestVenueSectorManagement:
    """Tests for venue sector CRUD endpoints."""

    def test_list_sectors_with_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that listing sectors includes nested seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Balcony")
        VenueSeat.objects.create(sector=sector, label="A1", row="A", number=1)
        VenueSeat.objects.create(sector=sector, label="A2", row="A", number=2)

        url = reverse("api:list_venue_sectors", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Balcony"
        assert len(data[0]["seats"]) == 2

    def test_create_sector_without_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test creating a sector without any seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "General Admission", "capacity": 500, "display_order": 1}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "General Admission"
        assert data["capacity"] == 500
        assert data["seats"] == []

    def test_create_sector_with_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test creating a sector with nested seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {
            "name": "Orchestra",
            "seats": [
                {"label": "A1", "row": "A", "number": 1},
                {"label": "A2", "row": "A", "number": 2},
                {"label": "B1", "row": "B", "number": 1, "is_accessible": True},
            ],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Orchestra"
        assert len(data["seats"]) == 3

        # Verify seats were created in DB
        sector = VenueSector.objects.get(venue=venue, name="Orchestra")
        assert sector.seats.count() == 3
        assert sector.seats.filter(is_accessible=True).count() == 1

    def test_create_sector_with_shape_and_valid_seat_positions(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test creating a sector with shape and seats with valid positions inside the shape."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        # Square shape from (0,0) to (100,100)
        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {
            "name": "Floor",
            "shape": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
            "seats": [
                {"label": "A1", "position": {"x": 50, "y": 50}},  # Inside
                {"label": "A2", "position": {"x": 10, "y": 10}},  # Inside
            ],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert len(data["seats"]) == 2

    def test_create_sector_with_shape_and_invalid_seat_position(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that creating a sector with seat position outside shape fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")

        # Square shape from (0,0) to (100,100)
        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {
            "name": "Floor",
            "shape": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
            "seats": [
                {"label": "A1", "position": {"x": 150, "y": 50}},  # Outside!
            ],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Schema validation returns 422 for invalid input
        assert response.status_code == 422
        assert "outside the sector shape" in response.json()["detail"][0]["msg"]

    def test_create_sector_duplicate_name_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that creating a sector with duplicate name in same venue fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        VenueSector.objects.create(venue=venue, name="Balcony")

        url = reverse("api:create_venue_sector", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Balcony"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_get_sector_with_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test getting a single sector with its seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="VIP")
        VenueSeat.objects.create(sector=sector, label="V1")
        VenueSeat.objects.create(sector=sector, label="V2")

        url = reverse(
            "api:get_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "VIP"
        assert len(data["seats"]) == 2

    def test_update_sector_metadata(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating sector metadata without touching seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Old Name", capacity=100)
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:update_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"name": "New Name", "capacity": 200}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data["capacity"] == 200
        # Seats should still exist
        assert len(data["seats"]) == 1

    def test_delete_sector(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test deleting a sector and its seats."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="To Delete")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:delete_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSector.objects.filter(id=sector.id).exists()
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_sector_metadata_in_response(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that sector metadata is included in API responses."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        metadata = {"aisle_positions": [{"x": 50, "y": 0}], "custom_key": "value"}
        sector = VenueSector.objects.create(venue=venue, name="Orchestra", metadata=metadata)

        url = reverse(
            "api:get_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        assert response.json()["metadata"] == metadata

    def test_create_sector_with_metadata(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test creating a sector with metadata via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        metadata = {"aisle_positions": [{"x": 50, "y": 0}], "label_offset": 10}

        url = reverse(
            "api:create_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id},
        )
        payload = {"name": "Orchestra", "metadata": metadata}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        assert response.json()["metadata"] == metadata

        sector = VenueSector.objects.get(venue=venue, name="Orchestra")
        assert sector.metadata == metadata

    def test_update_sector_metadata_field(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating a sector's metadata via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra", metadata={"old": "data"})
        new_metadata = {"new": "data", "nested": {"key": "value"}}

        url = reverse(
            "api:update_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"metadata": new_metadata}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["metadata"] == new_metadata

        sector.refresh_from_db()
        assert sector.metadata == new_metadata
