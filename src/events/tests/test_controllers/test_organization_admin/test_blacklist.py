"""Tests for organization admin blacklist endpoints."""

import uuid

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Blacklist, Organization, OrganizationStaff

pytestmark = pytest.mark.django_db


class TestListBlacklist:
    """Tests for listing blacklist entries."""

    def test_list_blacklist_by_owner(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test that owner can list blacklist entries."""
        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "results" in data

    def test_list_blacklist_by_staff_with_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff with manage_members permission can list blacklist."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 200

    def test_list_blacklist_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without manage_members permission cannot list blacklist."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 403

    def test_list_blacklist_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """Test that regular members cannot list blacklist entries."""
        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})
        response = member_client.get(url)
        assert response.status_code == 403


class TestCreateBlacklistEntry:
    """Tests for creating blacklist entries."""

    def test_create_blacklist_entry_manual_mode(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test creating a blacklist entry with manual fields."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {
            "email": "banned@example.com",
            "first_name": "Banned",
            "last_name": "User",
            "reason": "Violated terms of service",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "banned@example.com"
        assert data["first_name"] == "Banned"
        assert data["reason"] == "Violated terms of service"

    def test_create_blacklist_entry_by_user_id(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test creating a blacklist entry by user_id (quick mode)."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {
            "user_id": str(nonmember_user.id),
            "reason": "Spam behavior",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["user_id"] == str(nonmember_user.id)
        assert data["reason"] == "Spam behavior"

    def test_create_blacklist_entry_with_telegram(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test creating a blacklist entry with telegram username."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {
            "telegram_username": "banned_user",
            "first_name": "Banned",
            "reason": "Harassment",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["telegram_username"] == "banned_user"

    def test_create_blacklist_entry_nonexistent_user_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that creating blacklist entry with nonexistent user_id fails."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {
            "user_id": str(uuid.uuid4()),
            "reason": "Test",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404


class TestGetBlacklistEntry:
    """Tests for getting individual blacklist entries."""

    def test_get_blacklist_entry(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test getting a specific blacklist entry."""
        entry = Blacklist.objects.create(
            organization=organization,
            email="banned@example.com",
            first_name="Banned",
            reason="Test",
            created_by=organization_owner_user,
        )

        url = reverse("api:get_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": entry.id})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(entry.id)
        assert data["email"] == "banned@example.com"

    def test_get_blacklist_entry_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test getting a non-existent blacklist entry returns 404."""
        url = reverse("api:get_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": uuid.uuid4()})
        response = organization_owner_client.get(url)
        assert response.status_code == 404


class TestUpdateBlacklistEntry:
    """Tests for updating blacklist entries."""

    def test_update_blacklist_entry_reason(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test updating the reason of a blacklist entry."""
        entry = Blacklist.objects.create(
            organization=organization,
            email="banned@example.com",
            reason="Original reason",
            created_by=organization_owner_user,
        )

        url = reverse("api:update_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": entry.id})
        payload = {"reason": "Updated reason"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["reason"] == "Updated reason"

        entry.refresh_from_db()
        assert entry.reason == "Updated reason"

    def test_update_blacklist_entry_names(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test updating name fields of a blacklist entry."""
        entry = Blacklist.objects.create(
            organization=organization,
            first_name="Original",
            last_name="Name",
            created_by=organization_owner_user,
        )

        url = reverse("api:update_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": entry.id})
        payload = {"first_name": "Updated", "preferred_name": "Nick"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "Updated"
        assert data["preferred_name"] == "Nick"


class TestDeleteBlacklistEntry:
    """Tests for deleting blacklist entries."""

    def test_delete_blacklist_entry(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test deleting a blacklist entry."""
        entry = Blacklist.objects.create(
            organization=organization,
            email="banned@example.com",
            created_by=organization_owner_user,
        )

        url = reverse("api:delete_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": entry.id})
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not Blacklist.objects.filter(id=entry.id).exists()

    def test_delete_blacklist_entry_not_found(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test deleting a non-existent blacklist entry returns 404."""
        url = reverse("api:delete_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": uuid.uuid4()})
        response = organization_owner_client.delete(url)
        assert response.status_code == 404

    def test_delete_blacklist_entry_by_staff_with_permission(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that staff with manage_members permission can delete blacklist entries."""
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        entry = Blacklist.objects.create(
            organization=organization,
            email="banned@example.com",
            created_by=organization_owner_user,
        )

        url = reverse("api:delete_blacklist_entry", kwargs={"slug": organization.slug, "entry_id": entry.id})
        response = organization_staff_client.delete(url)

        assert response.status_code == 204
        assert not Blacklist.objects.filter(id=entry.id).exists()
