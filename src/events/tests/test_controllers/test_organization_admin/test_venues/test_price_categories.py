"""Tests for venue price category CRUD endpoints."""

import uuid

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import (
    Event,
    Organization,
    OrganizationStaff,
    PriceCategory,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)

pytestmark = pytest.mark.django_db


class TestPriceCategoryManagement:
    """Tests for price category CRUD endpoints."""

    def test_list_price_categories_by_staff(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff can list price categories, ordered by display_order."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=1)
        PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa", display_order=0)

        url = reverse("api:list_venue_price_categories", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = organization_staff_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert [c["name"] for c in data] == ["Standard", "Premium"]

    def test_list_price_categories_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Test that regular members cannot list price categories."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        url = reverse("api:list_venue_price_categories", kwargs={"slug": organization.slug, "venue_id": venue.id})
        response = member_client.get(url)

        assert response.status_code == 403

    def test_create_price_category_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that owner can create a price category (201)."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        url = reverse("api:create_venue_price_category", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Premium", "color": "#aa0000", "display_order": 2}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Premium"
        assert data["color"] == "#aa0000"
        assert data["display_order"] == 2
        assert PriceCategory.objects.filter(venue=venue, name="Premium").exists()

    def test_create_price_category_invalid_color(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that an invalid hex color is rejected with 422."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        url = reverse("api:create_venue_price_category", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Premium", "color": "red"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422

    def test_create_price_category_duplicate_name(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that a duplicate (venue, name) is rejected with 400."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")

        url = reverse("api:create_venue_price_category", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Premium", "color": "#bb0000"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_create_price_category_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without edit_organization permission cannot create categories."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        venue = Venue.objects.create(organization=organization, name="Hall")
        url = reverse("api:create_venue_price_category", kwargs={"slug": organization.slug, "venue_id": venue.id})
        payload = {"name": "Premium", "color": "#aa0000"}

        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403

    def test_update_price_category_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that owner can partially update a price category."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        category = PriceCategory.objects.create(venue=venue, name="Old", color="#aa0000")

        url = reverse(
            "api:update_venue_price_category",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "category_id": category.id},
        )
        payload = {"name": "New", "color": "#00aa00"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New"
        assert data["color"] == "#00aa00"
        category.refresh_from_db()
        assert category.name == "New"

    def test_delete_price_category_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that owner can delete an unreferenced category (204), unpainting seats."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        sector = VenueSector.objects.create(venue=venue, name="Stalls")
        category = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)

        url = reverse(
            "api:delete_venue_price_category",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "category_id": category.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not PriceCategory.objects.filter(id=category.id).exists()
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_delete_price_category_blocked_by_tier(
        self, organization_owner_client: Client, organization: Organization, event: Event
    ) -> None:
        """Test that deleting a tier-priced category is refused with 400."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        event.venue = venue
        event.save(update_fields=["venue"])
        sector = VenueSector.objects.create(venue=venue, name="Stalls")
        category = PriceCategory.objects.create(venue=venue, name="Gold", color="#ffaa00")
        TicketTier.objects.create(
            event=event,
            name="Gold",
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            category_prices={str(category.id): "10.00"},
        )

        url = reverse(
            "api:delete_venue_price_category",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "category_id": category.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 400
        assert PriceCategory.objects.filter(id=category.id).exists()

    def test_price_category_venue_not_found(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that operating on a non-existent venue returns 404."""
        url = reverse(
            "api:list_venue_price_categories",
            kwargs={"slug": organization.slug, "venue_id": uuid.uuid4()},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 404
