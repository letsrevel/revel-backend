"""Tests for organization admin announcement controller endpoints.

This module tests the announcement list and create endpoints, plus permission checks.
For CRUD operations (get, update, delete, send), see test_announcements_crud.py.
"""

from datetime import timedelta

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Announcement,
    Event,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
)

pytestmark = pytest.mark.django_db


class TestAnnouncementControllerFixtures:
    """Base fixtures for announcement controller tests."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner user."""
        return revel_user_factory(username="org_owner", email_verified=True)

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def owner_client(self, org_owner: RevelUser) -> Client:
        """Authenticated client for organization owner."""
        refresh = RefreshToken.for_user(org_owner)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def staff_user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Staff member user."""
        return revel_user_factory(username="staff_user")

    @pytest.fixture
    def staff_with_permission(
        self,
        org: Organization,
        staff_user: RevelUser,
    ) -> OrganizationStaff:
        """Staff member with send_announcements permission."""
        return OrganizationStaff.objects.create(
            organization=org,
            user=staff_user,
            permissions=PermissionsSchema(default=PermissionMap(send_announcements=True)).model_dump(mode="json"),
        )

    @pytest.fixture
    def staff_without_permission(
        self,
        org: Organization,
        staff_user: RevelUser,
    ) -> OrganizationStaff:
        """Staff member without send_announcements permission."""
        return OrganizationStaff.objects.create(
            organization=org,
            user=staff_user,
            permissions=PermissionsSchema(default=PermissionMap(send_announcements=False)).model_dump(mode="json"),
        )

    @pytest.fixture
    def staff_client(self, staff_user: RevelUser) -> Client:
        """Authenticated client for staff user."""
        refresh = RefreshToken.for_user(staff_user)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def member_user(
        self,
        org: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> RevelUser:
        """Regular organization member."""
        user = revel_user_factory(username="member_user")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def member_client(self, member_user: RevelUser) -> Client:
        """Authenticated client for regular member."""
        refresh = RefreshToken.for_user(member_user)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event in the organization."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
        )

    @pytest.fixture
    def membership_tier(self, org: Organization) -> MembershipTier:
        """Membership tier fixture."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Tier",
        )


class TestListAnnouncements(TestAnnouncementControllerFixtures):
    """Tests for GET /organization-admin/{slug}/announcements endpoint."""

    def test_owner_can_list_announcements(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that organization owner can list announcements."""
        # Arrange
        Announcement.objects.create(
            organization=org,
            title="Test Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["title"] == "Test Announcement"

    def test_staff_with_permission_can_list_announcements(
        self,
        staff_client: Client,
        staff_with_permission: OrganizationStaff,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that staff with permission can list announcements."""
        # Arrange
        Announcement.objects.create(
            organization=org,
            title="Test Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = staff_client.get(url)

        # Assert
        assert response.status_code == 200

    def test_staff_without_permission_cannot_list_announcements(
        self,
        staff_client: Client,
        staff_without_permission: OrganizationStaff,
        org: Organization,
    ) -> None:
        """Test that staff without permission cannot list announcements."""
        # Arrange
        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = staff_client.get(url)

        # Assert
        assert response.status_code == 403

    def test_member_cannot_list_announcements(
        self,
        member_client: Client,
        org: Organization,
    ) -> None:
        """Test that regular members cannot list announcements."""
        # Arrange
        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 403

    def test_unauthenticated_cannot_list_announcements(
        self,
        org: Organization,
    ) -> None:
        """Test that unauthenticated users cannot list announcements."""
        # Arrange
        client = Client()
        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = client.get(url)

        # Assert
        assert response.status_code == 401

    def test_list_announcements_filters_by_status(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test filtering announcements by status."""
        # Arrange
        Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )
        Announcement.objects.create(
            organization=org,
            title="Sent Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = owner_client.get(f"{url}?status=draft")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["status"] == "draft"

    def test_list_announcements_filters_by_event(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
    ) -> None:
        """Test filtering announcements by event."""
        # Arrange
        event_announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )
        Announcement.objects.create(
            organization=org,
            title="Members Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = owner_client.get(f"{url}?event_id={event.id}")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["id"] == str(event_announcement.id)


class TestCreateAnnouncement(TestAnnouncementControllerFixtures):
    """Tests for POST /organization-admin/{slug}/announcements endpoint."""

    def test_owner_can_create_announcement_with_event_targeting(
        self,
        owner_client: Client,
        org: Organization,
        event: Event,
    ) -> None:
        """Test creating announcement targeting event attendees."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Event Announcement",
            "body": "Hello attendees!",
            "event_id": str(event.id),
            "past_visibility": True,
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Event Announcement"
        assert data["event_id"] == str(event.id)
        assert data["status"] == "draft"

    def test_owner_can_create_announcement_with_all_members_targeting(
        self,
        owner_client: Client,
        org: Organization,
    ) -> None:
        """Test creating announcement targeting all members."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Members Announcement",
            "body": "Hello members!",
            "target_all_members": True,
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 201
        data = response.json()
        assert data["target_all_members"] is True

    def test_owner_can_create_announcement_with_tier_targeting(
        self,
        owner_client: Client,
        org: Organization,
        membership_tier: MembershipTier,
    ) -> None:
        """Test creating announcement targeting specific tiers."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "VIP Announcement",
            "body": "Hello VIPs!",
            "target_tier_ids": [str(membership_tier.id)],
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 201
        data = response.json()
        assert str(membership_tier.id) in [str(t["id"]) for t in data["target_tiers"]]

    def test_owner_can_create_announcement_with_staff_targeting(
        self,
        owner_client: Client,
        org: Organization,
    ) -> None:
        """Test creating announcement targeting staff only."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Staff Announcement",
            "body": "Hello staff!",
            "target_staff_only": True,
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 201
        data = response.json()
        assert data["target_staff_only"] is True

    def test_create_announcement_requires_exactly_one_targeting(
        self,
        owner_client: Client,
        org: Organization,
    ) -> None:
        """Test that exactly one targeting option must be selected."""
        # Arrange - No targeting option
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "No Target",
            "body": "Body",
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422

    def test_create_announcement_rejects_multiple_targeting(
        self,
        owner_client: Client,
        org: Organization,
        event: Event,
    ) -> None:
        """Test that multiple targeting options are rejected."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Multiple Targets",
            "body": "Body",
            "event_id": str(event.id),
            "target_all_members": True,
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422

    def test_staff_with_permission_can_create_announcement(
        self,
        staff_client: Client,
        staff_with_permission: OrganizationStaff,
        org: Organization,
    ) -> None:
        """Test that staff with permission can create announcements."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Staff Created",
            "body": "Body",
            "target_all_members": True,
        }

        # Act
        response = staff_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 201

    def test_staff_without_permission_cannot_create_announcement(
        self,
        staff_client: Client,
        staff_without_permission: OrganizationStaff,
        org: Organization,
    ) -> None:
        """Test that staff without permission cannot create announcements."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Should Not Create",
            "body": "Body",
            "target_all_members": True,
        }

        # Act
        response = staff_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 403

    def test_create_announcement_with_invalid_event_returns_422(
        self,
        owner_client: Client,
        org: Organization,
    ) -> None:
        """Test that creating with non-existent event returns 422."""
        from uuid import uuid4

        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Invalid Event Announcement",
            "body": "Body",
            "event_id": str(uuid4()),  # Non-existent event
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422
        assert "Event not found" in response.json()["detail"]

    def test_create_announcement_with_other_org_event_returns_422(
        self,
        owner_client: Client,
        org: Organization,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that creating with event from another organization returns 422."""
        # Arrange - Create another org with an event
        other_owner = revel_user_factory(username="other_owner")
        other_org = Organization.objects.create(
            name="Other Org",
            slug="other-org",
            owner=other_owner,
        )
        other_event = Event.objects.create(
            organization=other_org,
            name="Other Event",
            slug="other-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
        )

        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Wrong Org Event Announcement",
            "body": "Body",
            "event_id": str(other_event.id),
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422
        assert "Event not found" in response.json()["detail"]
