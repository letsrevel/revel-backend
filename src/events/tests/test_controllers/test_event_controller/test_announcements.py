"""Tests for public event announcements endpoint.

This module tests the GET /events/{event_id}/announcements endpoint
which allows authenticated users to view announcements for events they attend.
"""

from datetime import timedelta

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
    EventRSVP,
    Organization,
    Ticket,
    TicketTier,
)
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestEventAnnouncementsEndpoint:
    """Tests for GET /events/{event_id}/announcements endpoint."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner user."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Organization",
            slug="test-org",
            owner=org_owner,
            visibility=Organization.Visibility.PUBLIC,
        )

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test public event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    @pytest.fixture
    def free_tier(self, event: Event) -> TicketTier:
        """Free ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="Free",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    @pytest.fixture
    def attendee(
        self,
        event: Event,
        free_tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> RevelUser:
        """User with active ticket to the event."""
        user = revel_user_factory(username="attendee")
        Ticket.objects.create(
            event=event,
            user=user,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Attendee",
        )
        return user

    @pytest.fixture
    def attendee_client(self, attendee: RevelUser) -> Client:
        """Authenticated client for attendee."""
        refresh = RefreshToken.for_user(attendee)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def rsvp_user(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> RevelUser:
        """User with YES RSVP to the event."""
        user = revel_user_factory(username="rsvp_user")
        EventRSVP.objects.create(
            event=event,
            user=user,
            status=EventRSVP.RsvpStatus.YES,
        )
        return user

    @pytest.fixture
    def rsvp_client(self, rsvp_user: RevelUser) -> Client:
        """Authenticated client for RSVP user."""
        refresh = RefreshToken.for_user(rsvp_user)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    @pytest.fixture
    def non_attendee(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """User with no relationship to the event."""
        return revel_user_factory(username="non_attendee")

    @pytest.fixture
    def non_attendee_client(self, non_attendee: RevelUser) -> Client:
        """Authenticated client for non-attendee."""
        refresh = RefreshToken.for_user(non_attendee)
        return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

    def test_attendee_sees_announcement_they_received(
        self,
        attendee_client: Client,
        attendee: RevelUser,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that attendee can see announcements they received notifications for.

        When an attendee received a notification for an announcement,
        they should be able to see it in the list.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Important update!",
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create notification for the attendee
        Notification.objects.create(
            user=attendee,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(announcement.id)},
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)
        assert data[0]["title"] == "Event Announcement"

    def test_new_attendee_sees_announcement_with_past_visibility(
        self,
        event: Event,
        free_tier: TicketTier,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that new attendees can see announcements with past_visibility enabled.

        If past_visibility is True, new attendees who joined after the announcement
        was sent should still see it.
        """
        # Arrange - Create announcement with past_visibility
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Past Visible Announcement",
            body="Body",
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create new attendee AFTER announcement was sent (no notification)
        new_attendee = revel_user_factory(username="new_attendee")
        Ticket.objects.create(
            event=event,
            user=new_attendee,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="New Attendee",
        )

        refresh = RefreshToken.for_user(new_attendee)
        new_attendee_client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = new_attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)

    def test_new_attendee_does_not_see_announcement_without_past_visibility(
        self,
        event: Event,
        free_tier: TicketTier,
        org: Organization,
        org_owner: RevelUser,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that new attendees cannot see announcements without past_visibility.

        If past_visibility is False, new attendees who didn't receive the notification
        should not see the announcement.
        """
        # Arrange - Create announcement WITHOUT past_visibility
        Announcement.objects.create(
            organization=org,
            event=event,
            title="No Past Visibility",
            body="Body",
            past_visibility=False,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create new attendee AFTER announcement was sent (no notification)
        new_attendee = revel_user_factory(username="new_attendee")
        Ticket.objects.create(
            event=event,
            user=new_attendee,
            tier=free_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="New Attendee",
        )

        refresh = RefreshToken.for_user(new_attendee)
        new_attendee_client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = new_attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_non_attendee_sees_no_announcements(
        self,
        non_attendee_client: Client,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that non-attendees cannot see event announcements.

        Users who are not attending the event should not see any announcements,
        even with past_visibility enabled.
        """
        # Arrange
        Announcement.objects.create(
            organization=org,
            event=event,
            title="Event Announcement",
            body="Body",
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = non_attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_draft_announcements_not_visible(
        self,
        attendee_client: Client,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that draft announcements are not visible to attendees.

        Only SENT announcements should appear in the list.
        """
        # Arrange
        Announcement.objects.create(
            organization=org,
            event=event,
            title="Draft Announcement",
            body="Body",
            created_by=org_owner,
            status=Announcement.Status.DRAFT,
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_announcements_ordered_by_sent_date_newest_first(
        self,
        attendee_client: Client,
        attendee: RevelUser,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that announcements are ordered by sent_at descending.

        Newest announcements should appear first.
        """
        # Arrange
        old_announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Old Announcement",
            body="Body",
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now() - timedelta(days=2),
        )
        new_announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="New Announcement",
            body="Body",
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        # Create notifications for both
        Notification.objects.create(
            user=attendee,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(old_announcement.id)},
        )
        Notification.objects.create(
            user=attendee,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(new_announcement.id)},
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == str(new_announcement.id)  # Newest first
        assert data[1]["id"] == str(old_announcement.id)

    def test_rsvp_user_can_see_announcements(
        self,
        rsvp_client: Client,
        rsvp_user: RevelUser,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that users with YES RSVP can see announcements.

        RSVP YES users should be treated as attendees for visibility purposes.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="RSVP Announcement",
            body="Body",
            past_visibility=True,
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = rsvp_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(announcement.id)

    def test_unauthenticated_user_gets_401(
        self,
        client: Client,
        event: Event,
    ) -> None:
        """Test that unauthenticated users get 401."""
        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = client.get(url)

        # Assert
        assert response.status_code == 401

    def test_nonexistent_event_returns_404(
        self,
        attendee_client: Client,
    ) -> None:
        """Test that non-existent event returns 404."""
        from uuid import uuid4

        url = reverse("api:event_announcements", kwargs={"event_id": uuid4()})

        # Act
        response = attendee_client.get(url)

        # Assert
        assert response.status_code == 404

    def test_response_includes_organization_name(
        self,
        attendee_client: Client,
        attendee: RevelUser,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that response includes organization name for display.

        The public schema should include organization name.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Test Announcement",
            body="Body",
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )
        Notification.objects.create(
            user=attendee,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(announcement.id)},
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data[0]["organization_name"] == org.name

    def test_response_includes_event_name(
        self,
        attendee_client: Client,
        attendee: RevelUser,
        event: Event,
        org: Organization,
        org_owner: RevelUser,
    ) -> None:
        """Test that response includes event name for display.

        The public schema should include event name for event-targeted announcements.
        """
        # Arrange
        announcement = Announcement.objects.create(
            organization=org,
            event=event,
            title="Test Announcement",
            body="Body",
            created_by=org_owner,
            status=Announcement.Status.SENT,
            sent_at=timezone.now(),
        )
        Notification.objects.create(
            user=attendee,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context={"announcement_id": str(announcement.id)},
        )

        url = reverse("api:event_announcements", kwargs={"event_id": event.id})

        # Act
        response = attendee_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data[0]["event_name"] == event.name
