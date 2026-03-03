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


def test_delete_staff_granting_token_by_owner_succeeds(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Organization owner can delete a staff-granting token."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    staff_token = OrganizationToken.objects.create(
        organization=organization,
        name="Staff Token",
        issuer=organization_owner_user,
        grants_staff_status=True,
        grants_membership=True,
        membership_tier=default_tier,
    )
    url = reverse("api:delete_organization_token", kwargs={"slug": organization.slug, "token_id": staff_token.id})
    response = organization_owner_client.delete(url)
    assert response.status_code == 204
    assert not OrganizationToken.objects.filter(id=staff_token.id).exists()


def test_delete_staff_granting_token_by_non_owner_returns_403(
    organization: Organization, organization_staff_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """Staff with manage_members permission cannot delete a staff-granting token."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    staff_token = OrganizationToken.objects.create(
        organization=organization,
        name="Staff Token",
        issuer=organization_owner_user,
        grants_staff_status=True,
        grants_membership=True,
        membership_tier=default_tier,
    )
    staff_client = _make_staff_client(organization, organization_staff_user)
    url = reverse("api:delete_organization_token", kwargs={"slug": organization.slug, "token_id": staff_token.id})
    response = staff_client.delete(url)
    assert response.status_code == 403
    assert OrganizationToken.objects.filter(id=staff_token.id).exists()


def test_delete_membership_only_token_by_non_owner_succeeds(
    organization: Organization, organization_staff_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """Staff with manage_members permission can delete a membership-only token (no staff grant)."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    member_token = OrganizationToken.objects.create(
        organization=organization,
        name="Member Token",
        issuer=organization_owner_user,
        grants_staff_status=False,
        grants_membership=True,
        membership_tier=default_tier,
    )
    staff_client = _make_staff_client(organization, organization_staff_user)
    url = reverse("api:delete_organization_token", kwargs={"slug": organization.slug, "token_id": member_token.id})
    response = staff_client.delete(url)
    assert response.status_code == 204
    assert not OrganizationToken.objects.filter(id=member_token.id).exists()


# --- Tests for M-02: at least one grant must be enabled ---


def test_create_token_with_grants_membership_only_succeeds(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Token with only grants_membership=True can be created."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "Member Token",
        "grants_membership": True,
        "grants_staff_status": False,
        "membership_tier_id": str(default_tier.id),
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["grants_membership"] is True
    assert data["grants_staff_status"] is False


def test_create_token_with_grants_staff_status_only_succeeds(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Token with only grants_staff_status=True can be created."""
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "Staff Only Token",
        "grants_membership": False,
        "grants_staff_status": True,
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["grants_membership"] is False
    assert data["grants_staff_status"] is True


def test_create_token_with_both_grants_succeeds(organization_owner_client: Client, organization: Organization) -> None:
    """Token with both grants_membership=True and grants_staff_status=True can be created."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "Full Access Token",
        "grants_membership": True,
        "grants_staff_status": True,
        "membership_tier_id": str(default_tier.id),
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["grants_membership"] is True
    assert data["grants_staff_status"] is True


def test_create_token_with_no_grants_returns_422(organization_owner_client: Client, organization: Organization) -> None:
    """Token with both grants_membership=False and grants_staff_status=False cannot be created."""
    url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
    payload = {
        "name": "Useless Token",
        "grants_membership": False,
        "grants_staff_status": False,
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 422


def test_update_token_to_no_grants_both_explicit_returns_422(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Updating a token with both grants explicitly set to False is rejected."""
    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id})
    payload = {
        "grants_membership": False,
        "grants_staff_status": False,
    }
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 422
    # Verify the token was not modified
    organization_token.refresh_from_db()
    assert organization_token.grants_membership is True


def test_update_token_disable_membership_when_staff_already_false_returns_422(
    organization_owner_client: Client, organization: Organization, organization_token: OrganizationToken
) -> None:
    """Disabling grants_membership on a token that already has grants_staff_status=False is rejected."""
    # The default organization_token has grants_membership=True, grants_staff_status=False
    assert organization_token.grants_membership is True
    assert organization_token.grants_staff_status is False

    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": organization_token.id})
    payload = {"grants_membership": False}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 422
    # Verify the token was not modified
    organization_token.refresh_from_db()
    assert organization_token.grants_membership is True


def test_update_token_disable_staff_when_membership_true_succeeds(
    organization_owner_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
) -> None:
    """Disabling grants_staff_status on a token that has grants_membership=True succeeds."""
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    staff_token = OrganizationToken.objects.create(
        organization=organization,
        name="Staff Token",
        issuer=organization_owner_user,
        grants_staff_status=True,
        grants_membership=True,
        membership_tier=default_tier,
    )
    url = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": staff_token.id})
    payload = {"grants_staff_status": False}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["grants_staff_status"] is False
    assert data["grants_membership"] is True
