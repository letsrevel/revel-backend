"""Tests for organization admin token endpoints."""

from datetime import timedelta

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationStaff,
    OrganizationToken,
    PermissionMap,
    PermissionsSchema,
)

pytestmark = pytest.mark.django_db


def test_list_organization_tokens(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can list tokens."""
    url = reverse("api:list_organization_tokens", kwargs={"slug": organization.slug})
    response = organization_owner_client.get(url)
    assert response.status_code == 200


def test_create_organization_token(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can create a token."""
    # Get the default tier
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")

    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "New Token",
        "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
        "membership_tier_id": str(default_tier.id),
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Token"


def test_update_organization_token(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Test that an organization owner can update a token."""
    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id})
    payload = {"name": "Updated Token Name"}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Token Name"


def test_delete_organization_token(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Test that an organization owner can delete a token."""
    url = reverse(
        "api:delete_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id}
    )
    response = organization_owner_client.delete(url)
    assert response.status_code == 204
    assert not OrganizationToken.objects.filter(id=organization_token.id).exists()


# --- Tests for privilege escalation on grants_staff_status ---


def _make_staff_client(organization: Organization, user: RevelUser) -> Client:
    """Helper: create a staff member with manage_members permission and return their client."""
    OrganizationStaff.objects.create(
        organization=organization,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap(manage_members=True)).model_dump(mode="json"),
    )
    token = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(token.access_token)}")  # type: ignore[attr-defined]


def test_create_staff_granting_token_by_non_owner_returns_403(
    organization: Organization, organization_staff_user: RevelUser
) -> None:
    """Staff with manage_members permission cannot create staff-granting tokens."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    staff_client = _make_staff_client(organization, organization_staff_user)
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "Staff Token",
        "grants_staff_status": True,
        "grants_membership": True,
        "membership_tier_id": str(default_tier.id),
    }
    response = staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 403


def test_create_staff_granting_token_by_owner_succeeds(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Organization owner can create tokens that grant staff status."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "Staff Token",
        "grants_staff_status": True,
        "grants_membership": True,
        "membership_tier_id": str(default_tier.id),
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    assert response.json()["grants_staff_status"] is True


def test_update_token_to_grant_staff_status_by_non_owner_returns_403(
    organization: Organization, organization_staff_user: RevelUser, organization_token: OrganizationToken
) -> None:
    """Staff with manage_members permission cannot update a token to grant staff status."""
    staff_client = _make_staff_client(organization, organization_staff_user)
    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id})
    payload = {"grants_staff_status": True}
    response = staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 403
    organization_token.refresh_from_db()
    assert not organization_token.grants_staff_status


def test_update_token_to_grant_staff_status_by_owner_succeeds(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Organization owner can update a token to grant staff status."""
    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id})
    payload = {"grants_staff_status": True}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    assert response.json()["grants_staff_status"] is True
