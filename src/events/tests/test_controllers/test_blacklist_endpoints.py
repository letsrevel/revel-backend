# src/events/tests/test_controllers/test_blacklist_endpoints.py

"""Tests for blacklist and whitelist API endpoints.

Tests cover:
- Blacklist CRUD operations (org admin)
- Whitelist request management (org admin)
- User-facing whitelist request creation
- Permission enforcement
"""

from unittest.mock import patch

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Blacklist, Organization, WhitelistRequest

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def blacklist_target_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User to be blacklisted in tests."""
    return django_user_model.objects.create_user(
        username="target",
        email="target@example.com",
        password="pass",
        first_name="Target",
        last_name="User",
    )


@pytest.fixture
def public_user_client(public_user: RevelUser) -> Client:
    """Client for a public user (non-admin)."""
    refresh = RefreshToken.for_user(public_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# --- Blacklist List Endpoint Tests ---


class TestListBlacklistEntries:
    """Tests for GET /organizations/{slug}/blacklist/."""

    def test_lists_blacklist_entries(
        self,
        organization_owner_client: Client,
        organization: Organization,
        blacklist_target_user: RevelUser,
    ) -> None:
        """Owner should be able to list blacklist entries."""
        # Create some entries
        Blacklist.objects.create(
            organization=organization,
            email="bad1@example.com",
            created_by=organization.owner,
        )
        Blacklist.objects.create(
            organization=organization,
            user=blacklist_target_user,
            created_by=organization.owner,
        )

        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_filters_by_has_user(
        self,
        organization_owner_client: Client,
        organization: Organization,
        blacklist_target_user: RevelUser,
    ) -> None:
        """Should filter entries by whether they're linked to a user."""
        # Entry with user
        Blacklist.objects.create(
            organization=organization,
            user=blacklist_target_user,
            created_by=organization.owner,
        )
        # Entry without user
        Blacklist.objects.create(
            organization=organization,
            email="unlinked@example.com",
            created_by=organization.owner,
        )

        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})

        # Filter has_user=true
        response = organization_owner_client.get(url, {"has_user": "true"})
        assert response.status_code == 200
        assert response.json()["count"] == 1

        # Filter has_user=false
        response = organization_owner_client.get(url, {"has_user": "false"})
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_unauthorized_user_denied(
        self, public_user_client: Client, organization: Organization, public_user: RevelUser
    ) -> None:
        """Non-admin member should be denied access to blacklist."""
        from events.models import OrganizationMember

        # Make user a member so they can see the org (but not admin)
        OrganizationMember.objects.create(organization=organization, user=public_user)

        url = reverse("api:list_blacklist_entries", kwargs={"slug": organization.slug})
        response = public_user_client.get(url)

        assert response.status_code == 403


# --- Blacklist Create Endpoint Tests ---


class TestCreateBlacklistEntry:
    """Tests for POST /organizations/{slug}/blacklist/."""

    def test_creates_entry_manual_mode(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should create entry with manual identifiers."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {
            "email": "newbad@example.com",
            "reason": "Spamming",
            "first_name": "Bad",
            "last_name": "Actor",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "newbad@example.com"
        assert data["reason"] == "Spamming"
        assert Blacklist.objects.filter(email="newbad@example.com").exists()

    def test_creates_entry_quick_mode(
        self,
        organization_owner_client: Client,
        organization: Organization,
        blacklist_target_user: RevelUser,
    ) -> None:
        """Should create entry from user ID (quick mode)."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {
            "user_id": str(blacklist_target_user.id),
            "reason": "Quick blacklist test",
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert data["user_id"] == str(blacklist_target_user.id)
        assert data["email"] == blacklist_target_user.email

    def test_returns_400_no_identifiers(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should return 400 when no identifiers provided."""
        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {"reason": "No identifiers"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_returns_400_duplicate_email(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should return 400 when email already blacklisted."""
        # Create first entry
        Blacklist.objects.create(
            organization=organization,
            email="duplicate@example.com",
            created_by=organization.owner,
        )

        url = reverse("api:create_blacklist_entry", kwargs={"slug": organization.slug})
        payload = {"email": "duplicate@example.com"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400


# --- Blacklist Get/Update/Delete Endpoint Tests ---


class TestBlacklistEntryCRUD:
    """Tests for single blacklist entry operations."""

    def test_get_blacklist_entry(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should retrieve a single blacklist entry."""
        entry = Blacklist.objects.create(
            organization=organization,
            email="getme@example.com",
            reason="Test entry",
            created_by=organization.owner,
        )

        url = reverse(
            "api:get_blacklist_entry",
            kwargs={"slug": organization.slug, "entry_id": entry.id},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        assert response.json()["email"] == "getme@example.com"

    def test_update_blacklist_entry(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should update reason and name fields."""
        entry = Blacklist.objects.create(
            organization=organization,
            email="update@example.com",
            reason="Old reason",
            first_name="Old",
            created_by=organization.owner,
        )

        url = reverse(
            "api:update_blacklist_entry",
            kwargs={"slug": organization.slug, "entry_id": entry.id},
        )
        payload = {"reason": "New reason", "first_name": "New"}

        response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        entry.refresh_from_db()
        assert entry.reason == "New reason"
        assert entry.first_name == "New"

    def test_delete_blacklist_entry(self, organization_owner_client: Client, organization: Organization) -> None:
        """Should delete a blacklist entry."""
        entry = Blacklist.objects.create(
            organization=organization,
            email="delete@example.com",
            created_by=organization.owner,
        )
        entry_id = entry.id

        url = reverse(
            "api:delete_blacklist_entry",
            kwargs={"slug": organization.slug, "entry_id": entry.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not Blacklist.objects.filter(id=entry_id).exists()


# --- Whitelist Request Management Tests ---


class TestWhitelistRequestManagement:
    """Tests for whitelist request listing and management."""

    def test_list_whitelist_requests(
        self,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should list whitelist requests for the organization."""
        WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            message="Please whitelist me",
        )

        url = reverse("api:list_whitelist_requests", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_filter_whitelist_requests_by_status(
        self,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """Should filter whitelist requests by status."""
        other_user = django_user_model.objects.create_user(username="other", email="other@test.com")

        WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.PENDING,
        )
        WhitelistRequest.objects.create(
            organization=organization,
            user=other_user,
            status=WhitelistRequest.Status.APPROVED,
        )

        url = reverse("api:list_whitelist_requests", kwargs={"slug": organization.slug})

        # Filter pending
        response = organization_owner_client.get(url, {"status": "pending"})
        assert response.json()["count"] == 1

        # Filter approved
        response = organization_owner_client.get(url, {"status": "approved"})
        assert response.json()["count"] == 1

    @patch("events.service.whitelist_service.notification_requested")
    def test_approve_whitelist_request(
        self,
        mock_notification: object,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should approve a whitelist request and create whitelist entry."""
        blacklist_entry = Blacklist.objects.create(
            organization=organization,
            first_name="John",
            created_by=organization.owner,
        )
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.PENDING,
        )
        request.matched_blacklist_entries.add(blacklist_entry)

        url = reverse(
            "api:approve_whitelist_request",
            kwargs={"slug": organization.slug, "request_id": request.id},
        )
        response = organization_owner_client.post(url)

        assert response.status_code == 204
        request.refresh_from_db()
        assert request.status == WhitelistRequest.Status.APPROVED

    @patch("events.service.whitelist_service.notification_requested")
    def test_reject_whitelist_request(
        self,
        mock_notification: object,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should reject a whitelist request."""
        request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse(
            "api:reject_whitelist_request",
            kwargs={"slug": organization.slug, "request_id": request.id},
        )
        response = organization_owner_client.post(url)

        assert response.status_code == 204
        request.refresh_from_db()
        assert request.status == WhitelistRequest.Status.REJECTED


# --- Whitelist Entry Management Tests ---


class TestWhitelistEntryManagement:
    """Tests for whitelist entry listing and deletion (APPROVED requests)."""

    def test_list_whitelist_entries(
        self,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should list whitelist entries (APPROVED requests)."""
        WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization.owner,
        )

        url = reverse("api:list_whitelist_entries", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_list_whitelist_excludes_pending_and_rejected(
        self,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """Should only list APPROVED requests, not PENDING or REJECTED."""
        # Create APPROVED request
        WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization.owner,
        )
        # Create PENDING request for another user
        pending_user = django_user_model.objects.create_user(
            username="pending_user", email="pending@example.com", password="pass"
        )
        WhitelistRequest.objects.create(
            organization=organization,
            user=pending_user,
            status=WhitelistRequest.Status.PENDING,
        )
        # Create REJECTED request for another user
        rejected_user = django_user_model.objects.create_user(
            username="rejected_user", email="rejected@example.com", password="pass"
        )
        WhitelistRequest.objects.create(
            organization=organization,
            user=rejected_user,
            status=WhitelistRequest.Status.REJECTED,
        )

        url = reverse("api:list_whitelist_entries", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1  # Only APPROVED

    def test_delete_whitelist_entry(
        self,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should delete a whitelist entry (APPROVED request)."""
        whitelist_request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization.owner,
        )

        url = reverse(
            "api:delete_whitelist_entry",
            kwargs={"slug": organization.slug, "entry_id": whitelist_request.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not WhitelistRequest.objects.filter(id=whitelist_request.id).exists()

    def test_delete_non_approved_entry_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should return 404 when trying to delete a non-approved request via whitelist endpoint."""
        pending_request = WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse(
            "api:delete_whitelist_entry",
            kwargs={"slug": organization.slug, "entry_id": pending_request.id},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 404


# --- User-Facing Whitelist Request Tests ---


class TestCreateWhitelistRequestEndpoint:
    """Tests for POST /organizations/{slug}/whitelist-request/ (user-facing)."""

    @patch("events.service.whitelist_service.notification_requested")
    def test_creates_whitelist_request(
        self,
        mock_notification: object,
        public_user_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """User should be able to create whitelist request when fuzzy matched."""
        # Set up user name to match blacklist entry
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        # Create fuzzy-match blacklist entry (no user FK)
        Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",
            created_by=organization.owner,
        )

        url = reverse("api:create_whitelist_request", kwargs={"slug": organization.slug})
        payload = {"message": "I am not the blacklisted person"}

        response = public_user_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        assert WhitelistRequest.objects.filter(organization=organization, user=public_user).exists()

    def test_returns_400_no_fuzzy_matches(
        self,
        public_user_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should return 400 when no fuzzy matches exist."""
        # User has different name from any blacklist entry
        public_user.first_name = "Unique"
        public_user.last_name = "Person"
        public_user.save()

        url = reverse("api:create_whitelist_request", kwargs={"slug": organization.slug})
        payload = {"message": "Test"}

        response = public_user_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_returns_400_already_whitelisted(
        self,
        public_user_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should return 400 when user is already whitelisted (has APPROVED request)."""
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        # Create blacklist entry
        Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",
            created_by=organization.owner,
        )

        # Already whitelisted (has APPROVED request)
        WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.APPROVED,
            decided_by=organization.owner,
        )

        url = reverse("api:create_whitelist_request", kwargs={"slug": organization.slug})
        payload = {"message": "Test"}

        response = public_user_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_returns_400_request_already_pending(
        self,
        public_user_client: Client,
        organization: Organization,
        public_user: RevelUser,
    ) -> None:
        """Should return 400 when pending request already exists."""
        public_user.first_name = "John"
        public_user.last_name = "Smith"
        public_user.save()

        Blacklist.objects.create(
            organization=organization,
            first_name="John",
            last_name="Smith",
            created_by=organization.owner,
        )

        # Already has pending request
        WhitelistRequest.objects.create(
            organization=organization,
            user=public_user,
            status=WhitelistRequest.Status.PENDING,
        )

        url = reverse("api:create_whitelist_request", kwargs={"slug": organization.slug})
        payload = {"message": "Test"}

        response = public_user_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
