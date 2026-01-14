"""Tests for organization admin whitelist endpoints."""

import uuid

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationStaff, WhitelistRequest

pytestmark = pytest.mark.django_db


class TestListWhitelistRequests:
    """Tests for listing whitelist requests."""

    def test_list_whitelist_requests_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that owner can list whitelist requests."""
        url = reverse("api:list_whitelist_requests", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "results" in data

    def test_list_whitelist_requests_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff with manage_members permission can list whitelist requests."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:list_whitelist_requests", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 200

    def test_list_whitelist_requests_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without manage_members permission cannot list whitelist requests."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:list_whitelist_requests", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 403

    def test_list_whitelist_requests_by_member_forbidden(
        self, member_client: Client, organization: Organization
    ) -> None:
        """Test that regular members cannot list whitelist requests."""
        url = reverse("api:list_whitelist_requests", kwargs={"slug": organization.slug})
        response = member_client.get(url)
        assert response.status_code == 403


class TestGetWhitelistRequest:
    """Tests for getting individual whitelist requests."""

    def test_get_whitelist_request(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test getting a specific whitelist request."""
        # Create a blacklist entry to create the fuzzy match scenario
        blacklist_entry = Blacklist.objects.create(
            organization=organization,
            first_name=nonmember_user.first_name,
            last_name=nonmember_user.last_name,
            created_by=organization_owner_user,
        )

        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.PENDING,
        )
        request.matched_blacklist_entries.add(blacklist_entry)

        url = reverse("api:get_whitelist_request", kwargs={"slug": organization.slug, "request_id": request.id})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(request.id)
        assert data["user_id"] == str(nonmember_user.id)
        assert data["status"] == "pending"

    def test_get_whitelist_request_not_found(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test getting a non-existent whitelist request returns 404."""
        url = reverse("api:get_whitelist_request", kwargs={"slug": organization.slug, "request_id": uuid.uuid4()})
        response = organization_owner_client.get(url)
        assert response.status_code == 404


class TestApproveWhitelistRequest:
    """Tests for approving whitelist requests."""

    def test_approve_whitelist_request(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test approving a whitelist request."""
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse("api:approve_whitelist_request", kwargs={"slug": organization.slug, "request_id": request.id})
        response = organization_owner_client.post(url)

        assert response.status_code == 204

        request.refresh_from_db()
        assert request.status == WhitelistRequest.Status.APPROVED

    def test_approve_whitelist_request_not_found(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test approving a non-existent whitelist request returns 404."""
        url = reverse("api:approve_whitelist_request", kwargs={"slug": organization.slug, "request_id": uuid.uuid4()})
        response = organization_owner_client.post(url)
        assert response.status_code == 404

    def test_approve_whitelist_request_by_staff_with_permission(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that staff with manage_members permission can approve whitelist requests."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse("api:approve_whitelist_request", kwargs={"slug": organization.slug, "request_id": request.id})
        response = organization_staff_client.post(url)

        assert response.status_code == 204


class TestRejectWhitelistRequest:
    """Tests for rejecting whitelist requests."""

    def test_reject_whitelist_request(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test rejecting a whitelist request."""
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse("api:reject_whitelist_request", kwargs={"slug": organization.slug, "request_id": request.id})
        response = organization_owner_client.post(url)

        assert response.status_code == 204

        request.refresh_from_db()
        assert request.status == WhitelistRequest.Status.REJECTED

    def test_reject_whitelist_request_not_found(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test rejecting a non-existent whitelist request returns 404."""
        url = reverse("api:reject_whitelist_request", kwargs={"slug": organization.slug, "request_id": uuid.uuid4()})
        response = organization_owner_client.post(url)
        assert response.status_code == 404


class TestListWhitelist:
    """Tests for listing approved whitelist entries."""

    def test_list_whitelist_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can list whitelisted users."""
        url = reverse("api:list_whitelist_entries", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "results" in data

    def test_list_whitelist_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff with manage_members permission can list whitelist."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:list_whitelist_entries", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 200

    def test_list_whitelist_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Test that regular members cannot list whitelist."""
        url = reverse("api:list_whitelist_entries", kwargs={"slug": organization.slug})
        response = member_client.get(url)
        assert response.status_code == 403


class TestDeleteWhitelistEntry:
    """Tests for deleting whitelist entries."""

    def test_delete_whitelist_entry(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test deleting a whitelist entry (approved request)."""
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization_owner_user,
        )

        url = reverse("api:delete_whitelist_entry", kwargs={"slug": organization.slug, "entry_id": request.id})
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        # The request should be deleted or status changed
        assert not WhitelistRequest.objects.filter(id=request.id, status=WhitelistRequest.Status.APPROVED).exists()

    def test_delete_whitelist_entry_not_found(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test deleting a non-existent whitelist entry returns 404."""
        url = reverse("api:delete_whitelist_entry", kwargs={"slug": organization.slug, "entry_id": uuid.uuid4()})
        response = organization_owner_client.delete(url)
        assert response.status_code == 404

    def test_delete_whitelist_entry_pending_not_found(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that deleting a pending (not approved) request returns 404."""
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse("api:delete_whitelist_entry", kwargs={"slug": organization.slug, "entry_id": request.id})
        response = organization_owner_client.delete(url)

        # Should return 404 because only APPROVED entries are whitelist entries
        assert response.status_code == 404

    def test_delete_whitelist_entry_by_staff_with_permission(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that staff with manage_members permission can delete whitelist entries."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        request = WhitelistRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization_owner_user,
        )

        url = reverse("api:delete_whitelist_entry", kwargs={"slug": organization.slug, "entry_id": request.id})
        response = organization_staff_client.delete(url)

        assert response.status_code == 204
