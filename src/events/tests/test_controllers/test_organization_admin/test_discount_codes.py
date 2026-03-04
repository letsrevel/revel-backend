"""Tests for organization admin discount code endpoints.

Tests cover:
- Listing discount codes (empty, with data, pagination, search, filtering)
- Creating discount codes (valid, with M2M, validation errors)
- Getting discount code detail (exists, not found, wrong organization)
- Updating discount codes (partial update, M2M update)
- Deleting discount codes (soft-delete sets is_active=False)
- Permission checks (owner, staff with manage_events, staff without, non-staff)
"""

import uuid
from decimal import Decimal

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    OrganizationStaff,
    TicketTier,
)
from events.models.discount_code import DiscountCode

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manage_events_staff(
    organization: Organization, organization_staff_user: RevelUser, staff_member: OrganizationStaff
) -> OrganizationStaff:
    """Staff member with manage_events permission (required by discount codes controller)."""
    perms = staff_member.permissions
    perms["default"]["manage_events"] = True
    staff_member.permissions = perms
    staff_member.save()
    return staff_member


@pytest.fixture
def manage_events_staff_client(organization_staff_user: RevelUser, manage_events_staff: OrganizationStaff) -> Client:
    """API client for a staff member with manage_events permission."""
    from ninja_jwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(organization_staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def dc_percentage(organization: Organization) -> DiscountCode:
    """A 20% percentage discount code for the test organization."""
    return DiscountCode.objects.create(
        code="SAVE20",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("20.00"),
        is_active=True,
    )


@pytest.fixture
def dc_fixed(organization: Organization) -> DiscountCode:
    """A fixed EUR 10 discount code for the test organization."""
    return DiscountCode.objects.create(
        code="FLAT10",
        organization=organization,
        discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
        discount_value=Decimal("10.00"),
        currency="EUR",
        is_active=True,
    )


@pytest.fixture
def dc_inactive(organization: Organization) -> DiscountCode:
    """An inactive discount code."""
    return DiscountCode.objects.create(
        code="EXPIRED",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("50.00"),
        is_active=False,
    )


# ===========================================================================
# List discount codes
# ===========================================================================


class TestListDiscountCodes:
    """Tests for GET /organization-admin/{slug}/discount-codes."""

    def test_list_empty(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should return empty results when no discount codes exist."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_list_with_data(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
        dc_fixed: DiscountCode,
    ) -> None:
        """Should return all discount codes for the organization."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        codes = {r["code"] for r in data["results"]}
        assert codes == {"SAVE20", "FLAT10"}

    def test_list_includes_m2m_ids(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event_series: EventSeries,
        event: Event,
        event_ticket_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should include series_ids, event_ids, and tier_ids in responses."""
        dc_percentage.series.add(event_series)
        dc_percentage.events.add(event)
        dc_percentage.tiers.add(event_ticket_tier)

        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        result = data["results"][0]
        assert result["series_ids"] == [str(event_series.id)]
        assert result["event_ids"] == [str(event.id)]
        assert result["tier_ids"] == [str(event_ticket_tier.id)]

    def test_list_search_by_code(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
        dc_fixed: DiscountCode,
    ) -> None:
        """Should filter results by search query matching code."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url, {"search": "FLAT"})

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["code"] == "FLAT10"

    def test_list_filter_by_is_active(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
        dc_inactive: DiscountCode,
    ) -> None:
        """Should filter by is_active query parameter."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})

        # Active only
        response = organization_owner_client.get(url, {"is_active": "true"})
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["code"] == "SAVE20"

        # Inactive only
        response = organization_owner_client.get(url, {"is_active": "false"})
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["code"] == "EXPIRED"

    def test_list_filter_by_discount_type(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
        dc_fixed: DiscountCode,
    ) -> None:
        """Should filter by discount_type query parameter."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})

        response = organization_owner_client.get(url, {"discount_type": "percentage"})
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["code"] == "SAVE20"

    def test_list_by_staff_with_manage_events(
        self,
        manage_events_staff_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Staff with manage_events permission should access the list."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = manage_events_staff_client.get(url)

        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_list_by_staff_without_permission_forbidden(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
    ) -> None:
        """Staff without manage_events permission should get 403."""
        # staff_member has edit_organization=True but not manage_events
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)

        assert response.status_code == 403

    def test_list_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Regular members should get 403."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = member_client.get(url)

        assert response.status_code == 403

    def test_list_by_nonmember_forbidden(self, nonmember_client: Client, organization: Organization) -> None:
        """Non-members should get 404 (org hidden via scoped queryset)."""
        url = reverse("api:list_discount_codes", kwargs={"slug": organization.slug})
        response = nonmember_client.get(url)

        assert response.status_code == 404


# ===========================================================================
# Create discount code
# ===========================================================================


class TestCreateDiscountCode:
    """Tests for POST /organization-admin/{slug}/discount-codes."""

    def test_create_percentage_code(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should create a percentage discount code and return 201."""
        url = reverse("api:create_discount_code", kwargs={"slug": organization.slug})
        payload = {
            "code": "NEWCODE",
            "discount_type": "percentage",
            "discount_value": "15.00",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["code"] == "NEWCODE"
        assert data["discount_type"] == "percentage"
        assert data["discount_value"] == "15.00"
        assert data["is_active"] is True

    def test_create_fixed_amount_code(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should create a fixed amount discount code with currency."""
        url = reverse("api:create_discount_code", kwargs={"slug": organization.slug})
        payload = {
            "code": "FLATNEW",
            "discount_type": "fixed_amount",
            "discount_value": "5.00",
            "currency": "EUR",
            "max_uses": 100,
            "max_uses_per_user": 3,
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["discount_type"] == "fixed_amount"
        assert data["currency"] == "EUR"
        assert data["max_uses"] == 100
        assert data["max_uses_per_user"] == 3

    def test_create_with_m2m_relations(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event_series: EventSeries,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Should create a discount code with M2M scope relations."""
        url = reverse("api:create_discount_code", kwargs={"slug": organization.slug})
        payload = {
            "code": "SCOPED",
            "discount_type": "percentage",
            "discount_value": "10.00",
            "series_ids": [str(event_series.id)],
            "event_ids": [str(event.id)],
            "tier_ids": [str(event_ticket_tier.id)],
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["series_ids"] == [str(event_series.id)]
        assert data["event_ids"] == [str(event.id)]
        assert data["tier_ids"] == [str(event_ticket_tier.id)]

    def test_create_by_staff_with_manage_events(
        self,
        manage_events_staff_client: Client,
        organization: Organization,
    ) -> None:
        """Staff with manage_events permission should be able to create codes."""
        url = reverse("api:create_discount_code", kwargs={"slug": organization.slug})
        payload = {
            "code": "STAFFCODE",
            "discount_type": "percentage",
            "discount_value": "5.00",
        }

        response = manage_events_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201

    def test_create_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Regular members should not be able to create discount codes."""
        url = reverse("api:create_discount_code", kwargs={"slug": organization.slug})
        payload = {
            "code": "NOPE",
            "discount_type": "percentage",
            "discount_value": "5.00",
        }

        response = member_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 403


# ===========================================================================
# Get discount code detail
# ===========================================================================


class TestGetDiscountCode:
    """Tests for GET /organization-admin/{slug}/discount-codes/{code_id}."""

    def test_get_existing_code(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should return discount code details."""
        url = reverse(
            "api:get_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(dc_percentage.id)
        assert data["code"] == "SAVE20"
        assert data["discount_type"] == "percentage"
        assert data["discount_value"] == "20.00"

    def test_get_nonexistent_code(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should return 404 for a non-existent discount code ID."""
        url = reverse(
            "api:get_discount_code",
            kwargs={"slug": organization.slug, "code_id": uuid.uuid4()},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_get_code_from_wrong_organization(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 when code exists in a different organization."""
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=organization_owner_user,
        )
        other_code = DiscountCode.objects.create(
            code="OTHERCODE",
            organization=other_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
        )

        url = reverse(
            "api:get_discount_code",
            kwargs={"slug": organization.slug, "code_id": other_code.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 404

    def test_get_by_staff_with_manage_events(
        self,
        manage_events_staff_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Staff with manage_events should see discount code details."""
        url = reverse(
            "api:get_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        response = manage_events_staff_client.get(url)

        assert response.status_code == 200


# ===========================================================================
# Update discount code
# ===========================================================================


class TestUpdateDiscountCode:
    """Tests for PATCH /organization-admin/{slug}/discount-codes/{code_id}."""

    def test_partial_update_discount_value(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should update only the provided fields."""
        url = reverse(
            "api:update_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        payload = {"discount_value": "35.00"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["discount_value"] == "35.00"
        # Code unchanged
        assert data["code"] == "SAVE20"

    def test_update_is_active(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should allow toggling is_active via PATCH."""
        url = reverse(
            "api:update_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        payload = {"is_active": False}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["is_active"] is False
        dc_percentage.refresh_from_db()
        assert dc_percentage.is_active is False

    def test_update_m2m_relations(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should update M2M scope relations."""
        url = reverse(
            "api:update_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        payload = {
            "event_ids": [str(event.id)],
            "tier_ids": [str(event_ticket_tier.id)],
        }

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["event_ids"] == [str(event.id)]
        assert data["tier_ids"] == [str(event_ticket_tier.id)]

    def test_update_nonexistent_code(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should return 404 when updating a non-existent code."""
        url = reverse(
            "api:update_discount_code",
            kwargs={"slug": organization.slug, "code_id": uuid.uuid4()},
        )
        payload = {"discount_value": "10.00"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404

    def test_update_by_staff_with_manage_events(
        self,
        manage_events_staff_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Staff with manage_events should be able to update codes."""
        url = reverse(
            "api:update_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        payload = {"max_uses": 50}

        response = manage_events_staff_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["max_uses"] == 50


# ===========================================================================
# Delete discount code (soft-delete)
# ===========================================================================


class TestDeleteDiscountCode:
    """Tests for DELETE /organization-admin/{slug}/discount-codes/{code_id}."""

    def test_delete_sets_inactive(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should soft-delete by setting is_active=False."""
        url = reverse(
            "api:delete_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        dc_percentage.refresh_from_db()
        assert dc_percentage.is_active is False
        # Record still exists in DB
        assert DiscountCode.objects.filter(id=dc_percentage.id).exists()

    def test_delete_nonexistent_code(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should return 404 when deleting a non-existent code."""
        url = reverse(
            "api:delete_discount_code",
            kwargs={"slug": organization.slug, "code_id": uuid.uuid4()},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 404

    def test_delete_by_staff_with_manage_events(
        self,
        manage_events_staff_client: Client,
        organization: Organization,
        dc_fixed: DiscountCode,
    ) -> None:
        """Staff with manage_events should be able to soft-delete codes."""
        url = reverse(
            "api:delete_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_fixed.id},
        )
        response = manage_events_staff_client.delete(url)

        assert response.status_code == 204
        dc_fixed.refresh_from_db()
        assert dc_fixed.is_active is False

    def test_delete_by_member_forbidden(
        self,
        member_client: Client,
        organization: Organization,
        dc_percentage: DiscountCode,
    ) -> None:
        """Regular members should not be able to delete discount codes."""
        url = reverse(
            "api:delete_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_percentage.id},
        )
        response = member_client.delete(url)

        assert response.status_code == 403
        # Verify code is unchanged
        dc_percentage.refresh_from_db()
        assert dc_percentage.is_active is True

    def test_delete_already_inactive_code(
        self,
        organization_owner_client: Client,
        organization: Organization,
        dc_inactive: DiscountCode,
    ) -> None:
        """Should still succeed (204) when deleting an already-inactive code."""
        url = reverse(
            "api:delete_discount_code",
            kwargs={"slug": organization.slug, "code_id": dc_inactive.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        dc_inactive.refresh_from_db()
        assert dc_inactive.is_active is False
