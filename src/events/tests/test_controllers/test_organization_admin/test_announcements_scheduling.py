"""Tests for announcement scheduling endpoints.

This module tests the schedule/unschedule endpoints and that update/delete
work on SCHEDULED announcements.
"""

from datetime import timedelta

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Announcement,
    Event,
    Organization,
)

pytestmark = pytest.mark.django_db


class TestAnnouncementSchedulingFixtures:
    """Base fixtures for announcement scheduling tests."""

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
        """Test event in the organization (starts in the future)."""
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
    def draft_members(self, org: Organization, org_owner: RevelUser) -> Announcement:
        """A member-targeted draft announcement."""
        return Announcement.objects.create(
            organization=org,
            title="Draft",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )

    @pytest.fixture
    def draft_event(self, org: Organization, org_owner: RevelUser, event: Event) -> Announcement:
        """An event-targeted draft announcement."""
        return Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Draft",
            body="Body",
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )


class TestScheduleAnnouncement(TestAnnouncementSchedulingFixtures):
    """Tests for POST /organization-admin/{slug}/announcements/{id}/schedule."""

    def test_schedule_with_absolute_time(
        self,
        owner_client: Client,
        org: Organization,
        draft_members: Announcement,
    ) -> None:
        """Scheduling with a future absolute time transitions to SCHEDULED."""
        scheduled_at = timezone.now() + timedelta(days=1)
        url = reverse(
            "api:schedule_announcement",
            kwargs={"slug": org.slug, "announcement_id": draft_members.id},
        )
        payload = {"scheduled_at": scheduled_at.isoformat()}

        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["status"] == "scheduled"
        assert data["scheduled_at"] is not None
        draft_members.refresh_from_db()
        assert draft_members.status == Announcement.AnnouncementStatus.SCHEDULED

    def test_schedule_with_relative_anchor(
        self,
        owner_client: Client,
        org: Organization,
        draft_event: Announcement,
    ) -> None:
        """Relative scheduling on an event-targeted draft persists the anchor."""
        url = reverse(
            "api:schedule_announcement",
            kwargs={"slug": org.slug, "announcement_id": draft_event.id},
        )
        payload = {
            "schedule_anchor": "event_start",
            "schedule_offset_minutes": -1440,
        }

        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["status"] == "scheduled"
        assert data["scheduled_at"] is None
        assert data["schedule_anchor"] == "event_start"
        assert data["schedule_offset_minutes"] == -1440
        draft_event.refresh_from_db()
        assert draft_event.status == Announcement.AnnouncementStatus.SCHEDULED
        assert draft_event.schedule_anchor == Announcement.ScheduleAnchor.EVENT_START

    def test_schedule_past_time_returns_422(
        self,
        owner_client: Client,
        org: Organization,
        draft_members: Announcement,
    ) -> None:
        """Scheduling with a past absolute time returns 422."""
        scheduled_at = timezone.now() - timedelta(days=1)
        url = reverse(
            "api:schedule_announcement",
            kwargs={"slug": org.slug, "announcement_id": draft_members.id},
        )
        payload = {"scheduled_at": scheduled_at.isoformat()}

        response = owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 422, response.content
        assert "future" in response.json()["detail"]
        draft_members.refresh_from_db()
        assert draft_members.status == Announcement.AnnouncementStatus.DRAFT


class TestUnscheduleAnnouncement(TestAnnouncementSchedulingFixtures):
    """Tests for POST /organization-admin/{slug}/announcements/{id}/unschedule."""

    def test_unschedule_reverts_to_draft(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Unscheduling a SCHEDULED announcement reverts it to DRAFT."""
        announcement = Announcement.objects.create(
            organization=org,
            title="Scheduled",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        url = reverse(
            "api:unschedule_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        response = owner_client.post(url, content_type="application/json")

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["status"] == "draft"
        assert data["scheduled_at"] is None
        announcement.refresh_from_db()
        assert announcement.status == Announcement.AnnouncementStatus.DRAFT


class TestEditScheduledAnnouncement(TestAnnouncementSchedulingFixtures):
    """Tests that SCHEDULED announcements remain editable/deletable."""

    def test_can_update_scheduled_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Updating a SCHEDULED announcement is allowed."""
        announcement = Announcement.objects.create(
            organization=org,
            title="Scheduled",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        url = reverse(
            "api:update_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )
        payload = {"title": "Updated Title"}

        response = owner_client.put(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["title"] == "Updated Title"

    def test_can_delete_scheduled_announcement(
        self,
        owner_client: Client,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Deleting a SCHEDULED announcement is allowed."""
        announcement = Announcement.objects.create(
            organization=org,
            title="Scheduled",
            body="Body",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            scheduled_at=timezone.now() + timedelta(days=1),
        )
        url = reverse(
            "api:delete_announcement",
            kwargs={"slug": org.slug, "announcement_id": announcement.id},
        )

        response = owner_client.delete(url)

        assert response.status_code == 204
        assert not Announcement.objects.filter(id=announcement.id).exists()
