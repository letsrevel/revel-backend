"""Tests for organization admin announcement controller endpoints.

This module tests the announcement management endpoints including CRUD operations,
sending announcements, and recipient count preview.
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
    Ticket,
    TicketTier,
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
        """Test that organization owner can list all announcements.

        Owners should see both draft and sent announcements.
        """
        # Arrange
        Announcement.objects.create(
            organization=org,
            title="Draft Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )
        Announcement.objects.create(
            organization=org,
            title="Sent Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2

    def test_staff_with_permission_can_list_announcements(
        self,
        staff_client: Client,
        staff_with_permission: OrganizationStaff,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that staff with send_announcements permission can list announcements."""
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
        data = response.json()
        assert data["count"] == 1

    def test_staff_without_permission_cannot_list_announcements(
        self,
        staff_client: Client,
        staff_without_permission: OrganizationStaff,
        org: Organization,
    ) -> None:
        """Test that staff without permission gets 403."""
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
        """Test that regular members cannot access admin announcements."""
        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act
        response = member_client.get(url)

        # Assert
        assert response.status_code == 403

    def test_unauthenticated_user_cannot_list_announcements(
        self,
        client: Client,
        org: Organization,
    ) -> None:
        """Test that unauthenticated users get 401."""
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
            title="Draft",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )
        Announcement.objects.create(
            organization=org,
            title="Sent",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:list_announcements", kwargs={"slug": org.slug})

        # Act - Filter by draft
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
        assert len(data["target_tiers"]) == 1
        assert data["target_tiers"][0]["id"] == str(membership_tier.id)

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

    def test_create_announcement_requires_exactly_one_targeting_option(
        self,
        owner_client: Client,
        org: Organization,
    ) -> None:
        """Test that exactly one targeting option must be provided."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "No Target Announcement",
            "body": "This should fail",
            # No targeting option provided
        }

        # Act
        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422  # Validation error

    def test_create_announcement_rejects_multiple_targeting_options(
        self,
        owner_client: Client,
        org: Organization,
        event: Event,
    ) -> None:
        """Test that multiple targeting options are rejected."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Multi Target Announcement",
            "body": "This should fail",
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
        assert response.status_code == 422  # Validation error

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
            "title": "Staff Created Announcement",
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
        """Test that staff without permission gets 403."""
        # Arrange
        url = reverse("api:create_announcement", kwargs={"slug": org.slug})
        payload = {
            "title": "Unauthorized Announcement",
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


class TestGetAnnouncement(TestAnnouncementControllerFixtures):
    """Tests for GET /organization-admin/{slug}/announcements/{id} endpoint."""

    def test_owner_can_get_announcement_details(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test getting announcement details."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Test Announcement",
            body="Body content",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse(
            "api:get_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(announcement.id)
        assert data["title"] == "Test Announcement"
        assert data["body"] == "Body content"
        assert data["created_by_name"] == org_owner.display_name

    def test_get_nonexistent_announcement_returns_404(
        self,
        owner_client: Client,
        org: Organization,
    ) -> None:
        """Test that getting non-existent announcement returns 404."""
        # Arrange
        from uuid import uuid4

        url = reverse(
            "api:get_announcement",
            kwargs={"slug": org.slug, "announcement_id": uuid4()},
        )

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 404


class TestUpdateAnnouncement(TestAnnouncementControllerFixtures):
    """Tests for PUT /organization-admin/{slug}/announcements/{id} endpoint."""

    def test_owner_can_update_draft_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test updating a draft announcement."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Original Title",
            body="Original body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse(
            "api:update_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )
        payload = {
            "title": "Updated Title",
            "body": "Updated body",
        }

        # Act
        response = owner_client.put(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["body"] == "Updated body"

    def test_cannot_update_sent_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that sent announcements cannot be updated."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Sent Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse(
            "api:update_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )
        payload = {"title": "Should Not Update"}

        # Act
        response = owner_client.put(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404  # Controller filters by DRAFT status

    def test_update_announcement_change_targeting(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
    ) -> None:
        """Test updating announcement targeting from members to event."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse(
            "api:update_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )
        payload = {
            "event_id": str(event.id),
            "target_all_members": False,
        }

        # Act
        response = owner_client.put(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["event_id"] == str(event.id)
        assert data["target_all_members"] is False

    def test_update_announcement_with_invalid_event_returns_422(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that updating with non-existent event returns 422."""
        from uuid import uuid4

        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse(
            "api:update_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )
        payload = {
            "event_id": str(uuid4()),  # Non-existent event
            "target_all_members": False,
        }

        # Act
        response = owner_client.put(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422
        assert "Event not found" in response.json()["detail"]

    def test_update_announcement_clearing_all_targeting_returns_422(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that clearing all targeting options returns 422.

        When an update explicitly disables targeting without enabling another,
        the schema validation should reject it.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
        )

        url = reverse(
            "api:update_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )
        payload = {
            "target_all_members": False,  # Clearing without setting another target
        }

        # Act
        response = owner_client.put(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422


class TestDeleteAnnouncement(TestAnnouncementControllerFixtures):
    """Tests for DELETE /organization-admin/{slug}/announcements/{id} endpoint."""

    def test_owner_can_delete_draft_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that owner can delete draft announcements."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="To Delete",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse(
            "api:delete_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.delete(url)

        # Assert
        assert response.status_code == 204
        assert not Announcement.objects.filter(id=announcement.id).exists()

    def test_cannot_delete_sent_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that sent announcements cannot be deleted."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Sent Announcement",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse(
            "api:delete_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.delete(url)

        # Assert
        assert response.status_code == 404  # Controller filters by DRAFT status
        assert Announcement.objects.filter(id=announcement.id).exists()


class TestSendAnnouncement(TestAnnouncementControllerFixtures):
    """Tests for POST /organization-admin/{slug}/announcements/{id}/send endpoint."""

    def test_owner_can_send_draft_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test sending a draft announcement to recipients."""
        # Arrange - Create member to receive announcement
        member = revel_user_factory(username="member")
        OrganizationMember.objects.create(
            organization=org,
            user=member,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        announcement = Announcement.objects.create(
            organization=org,
            title="To Send",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse(
            "api:send_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.post(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "sent"
        assert data["sent_at"] is not None
        assert data["recipient_count"] == 1

    def test_cannot_send_already_sent_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that already sent announcements cannot be sent again."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Already Sent",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse(
            "api:send_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.post(url)

        # Assert
        assert response.status_code == 404  # Controller filters by DRAFT status

    def test_send_announcement_with_no_recipients_succeeds(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that sending with no recipients still succeeds."""
        # Arrange - Create announcement targeting non-existent members
        announcement = Announcement.objects.create(
            organization=org,
            title="No Recipients",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse(
            "api:send_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.post(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "sent"
        assert data["recipient_count"] == 0


class TestGetRecipientCount(TestAnnouncementControllerFixtures):
    """Tests for GET /organization-admin/{slug}/announcements/{id}/recipient-count endpoint."""

    def test_get_recipient_count_for_draft(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test getting recipient count for draft announcement."""
        # Arrange - Create members
        for i in range(3):
            member = revel_user_factory(username=f"member_{i}")
            OrganizationMember.objects.create(
                organization=org,
                user=member,
                status=OrganizationMember.MembershipStatus.ACTIVE,
            )

        announcement = Announcement.objects.create(
            organization=org,
            title="Draft",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse(
            "api:get_announcement_recipient_count",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3

    def test_get_recipient_count_for_sent_returns_stored_count(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that sent announcements return stored recipient_count."""
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            title="Sent",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
            recipient_count=42,  # Stored count
        )

        url = reverse(
            "api:get_announcement_recipient_count",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 42

    def test_get_recipient_count_for_event_targeting(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test getting recipient count for event-targeted announcement."""
        # Arrange - Create ticket holder
        free_tier = TicketTier.objects.create(
            event=event,
            name="Free",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        attendee = revel_user_factory(username="attendee")
        Ticket.objects.create(
            event=event,
            user=attendee,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Attendee",
        )

        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            created_by=org_owner,
        )

        url = reverse(
            "api:get_announcement_recipient_count",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        # Act
        response = owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
