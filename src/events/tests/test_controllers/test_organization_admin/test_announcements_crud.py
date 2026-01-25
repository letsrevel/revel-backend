"""Tests for announcement CRUD and send operations.

This module tests get, update, delete, send, and recipient count endpoints.
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
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


class TestAnnouncementCrudFixtures:
    """Base fixtures for announcement CRUD tests."""

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


class TestGetAnnouncement(TestAnnouncementCrudFixtures):
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


class TestUpdateAnnouncement(TestAnnouncementCrudFixtures):
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
        """Test that clearing all targeting options returns 422."""
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


class TestDeleteAnnouncement(TestAnnouncementCrudFixtures):
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


class TestSendAnnouncement(TestAnnouncementCrudFixtures):
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


class TestGetRecipientCount(TestAnnouncementCrudFixtures):
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
