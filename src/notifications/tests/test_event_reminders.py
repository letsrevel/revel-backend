"""Tests for event reminder functionality."""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventRSVP, Organization, Ticket, TicketTier
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.reminder_service import EventReminderService
from notifications.tasks import send_event_reminders

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Organization owner."""
    return revel_user_factory()


@pytest.fixture
def organization(org_owner: RevelUser) -> Organization:
    """Test organization."""
    return Organization.objects.create(
        name="Test Org",
        slug="test-org",
        owner=org_owner,
    )


@pytest.fixture
def ticket_holder_1(revel_user_factory: RevelUserFactory) -> RevelUser:
    """First ticket holder."""
    user = revel_user_factory()
    # Enable event reminders
    prefs = user.notification_preferences
    prefs.event_reminders_enabled = True
    prefs.save()
    return user


@pytest.fixture
def ticket_holder_2(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Second ticket holder."""
    user = revel_user_factory()
    # Enable event reminders
    prefs = user.notification_preferences
    prefs.event_reminders_enabled = True
    prefs.save()
    return user


@pytest.fixture
def rsvp_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """RSVP user."""
    user = revel_user_factory()
    # Enable event reminders
    prefs = user.notification_preferences
    prefs.event_reminders_enabled = True
    prefs.save()
    return user


@pytest.fixture
def disabled_reminders_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """User with reminders disabled."""
    user = revel_user_factory()
    # Disable event reminders
    prefs = user.notification_preferences
    prefs.event_reminders_enabled = False
    prefs.save()
    return user


@pytest.fixture
def future_event_14_days(organization: Organization) -> Event:
    """Event happening in 14 days."""
    start = timezone.now() + timedelta(days=14)
    return Event.objects.create(
        organization=organization,
        name="Event in 14 Days",
        slug="event-14",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        requires_ticket=True,
    )


@pytest.fixture
def future_event_7_days(organization: Organization) -> Event:
    """Event happening in 7 days."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Event in 7 Days",
        slug="event-7",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        requires_ticket=True,
    )


@pytest.fixture
def future_event_1_day(organization: Organization) -> Event:
    """Event happening in 1 day."""
    start = timezone.now() + timedelta(days=1)
    return Event.objects.create(
        organization=organization,
        name="Event Tomorrow",
        slug="event-1",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        requires_ticket=True,
    )


@pytest.fixture
def rsvp_event(organization: Organization) -> Event:
    """RSVP-based event (no tickets required)."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="RSVP Event",
        slug="rsvp-event",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        requires_ticket=False,
    )


def get_or_create_ticket_tier(event: Event, name: str = "General Admission") -> TicketTier:
    """Helper to get or create ticket tiers (events auto-create default tiers)."""
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name=name,
        defaults={"price": 10.00},
    )
    return tier


class TestShouldSendReminder:
    """Test reminder eligibility check."""

    def test_returns_true_when_eligible(
        self,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that eligible users should receive reminders."""
        # Arrange
        event_id = "test-event-id"
        already_sent: set[tuple[t.Any, str]] = set()

        # Act
        service = EventReminderService()
        result = service.should_send_reminder(ticket_holder_1, event_id, already_sent)

        # Assert
        assert result is True

    def test_returns_false_when_reminders_disabled(
        self,
        disabled_reminders_user: RevelUser,
    ) -> None:
        """Test that users with reminders disabled don't receive them."""
        # Arrange
        event_id = "test-event-id"
        already_sent: set[tuple[t.Any, str]] = set()

        # Act
        service = EventReminderService()
        result = service.should_send_reminder(disabled_reminders_user, event_id, already_sent)

        # Assert
        assert result is False

    def test_returns_false_when_notification_type_disabled(
        self,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that users with EVENT_REMINDER type disabled don't receive them."""
        # Arrange
        event_id = "test-event-id"
        already_sent: set[tuple[t.Any, str]] = set()

        # Disable EVENT_REMINDER notification type
        prefs = ticket_holder_1.notification_preferences
        prefs.notification_type_settings[NotificationType.EVENT_REMINDER] = {
            "enabled": False,
            "channels": [],
        }
        prefs.save()

        # Act
        service = EventReminderService()
        result = service.should_send_reminder(ticket_holder_1, event_id, already_sent)

        # Assert
        assert result is False

    def test_returns_false_when_already_sent(
        self,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that reminders aren't sent twice for same event."""
        # Arrange
        event_id = "test-event-id"
        already_sent = {(ticket_holder_1.id, event_id)}

        # Act
        service = EventReminderService()
        result = service.should_send_reminder(ticket_holder_1, event_id, already_sent)

        # Assert
        assert result is False


class TestBuildEventContext:
    """Test event context building."""

    def test_builds_complete_context(
        self,
        future_event_14_days: Event,
    ) -> None:
        """Test that all required context fields are included.

        Note: event_location is NOT included in base context as it depends
        on per-user visibility permissions. It's added via _add_user_location_context.
        """
        # Arrange
        service = EventReminderService()
        days = 14

        # Act
        context = service.build_event_context(future_event_14_days, days)

        # Assert
        assert context["event_id"] == str(future_event_14_days.id)
        assert context["event_name"] == future_event_14_days.name
        assert context["event_start"] == future_event_14_days.start.isoformat()
        assert "event_start_formatted" in context
        assert "event_end_formatted" in context  # Event has end time by default
        # event_location is added per-user via _add_user_location_context
        assert "event_location" not in context
        assert "event_url" in context
        assert context["days_until"] == days


class TestSendTicketReminders:
    """Test ticket reminder sending."""

    @patch("notifications.signals.notification_requested.send")
    def test_sends_reminders_to_ticket_holders(
        self,
        mock_signal: MagicMock,
        future_event_14_days: Event,
        ticket_holder_1: RevelUser,
        ticket_holder_2: RevelUser,
    ) -> None:
        """Test that reminders are sent to all ticket holders."""
        # Arrange
        ticket_tier = get_or_create_ticket_tier(future_event_14_days)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_1,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_2,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        event_context = {
            "event_id": str(future_event_14_days.id),
            "event_name": future_event_14_days.name,
            "event_start": future_event_14_days.start.isoformat(),
            "event_start_formatted": "Test Date",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "days_until": 14,
        }
        already_sent: set[tuple[t.Any, str]] = set()
        sent_to_users: set[t.Any] = set()

        # Act
        service = EventReminderService()
        count, sent_to_users = service.send_ticket_reminders(future_event_14_days, event_context, already_sent)

        # Assert
        assert count == 2
        assert mock_signal.call_count == 2
        assert ticket_holder_1.id in sent_to_users
        assert ticket_holder_2.id in sent_to_users

    @patch("notifications.signals.notification_requested.send")
    def test_skips_users_with_reminders_disabled(
        self,
        mock_signal: MagicMock,
        future_event_14_days: Event,
        ticket_holder_1: RevelUser,
        disabled_reminders_user: RevelUser,
    ) -> None:
        """Test that users with disabled reminders are skipped."""
        # Arrange
        ticket_tier = get_or_create_ticket_tier(future_event_14_days)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_1,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=disabled_reminders_user,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        event_context = {
            "event_id": str(future_event_14_days.id),
            "event_name": future_event_14_days.name,
            "event_start": future_event_14_days.start.isoformat(),
            "event_start_formatted": "Test Date",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "days_until": 14,
        }
        already_sent: set[tuple[t.Any, str]] = set()
        sent_to_users: set[t.Any] = set()

        # Act
        service = EventReminderService()
        count, sent_to_users = service.send_ticket_reminders(future_event_14_days, event_context, already_sent)

        # Assert
        assert count == 1  # Only one reminder sent
        assert mock_signal.call_count == 1
        assert ticket_holder_1.id in sent_to_users
        assert disabled_reminders_user.id not in sent_to_users

    @patch("notifications.signals.notification_requested.send")
    def test_skips_already_sent_reminders(
        self,
        mock_signal: MagicMock,
        future_event_14_days: Event,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that duplicate reminders are prevented."""
        # Arrange
        ticket_tier = get_or_create_ticket_tier(future_event_14_days)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_1,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        event_context = {
            "event_id": str(future_event_14_days.id),
            "event_name": future_event_14_days.name,
            "event_start": future_event_14_days.start.isoformat(),
            "event_start_formatted": "Test Date",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "days_until": 14,
        }
        # Mark as already sent
        already_sent = {(ticket_holder_1.id, str(future_event_14_days.id))}
        sent_to_users: set[t.Any] = set()

        # Act
        service = EventReminderService()
        count, sent_to_users = service.send_ticket_reminders(future_event_14_days, event_context, already_sent)

        # Assert
        assert count == 0
        mock_signal.assert_not_called()

    @patch("notifications.signals.notification_requested.send")
    def test_includes_ticket_context(
        self,
        mock_signal: MagicMock,
        future_event_14_days: Event,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that ticket-specific context is included."""
        # Arrange
        ticket_tier = get_or_create_ticket_tier(future_event_14_days)
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_1,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        event_context = {
            "event_id": str(future_event_14_days.id),
            "event_name": future_event_14_days.name,
            "event_start": future_event_14_days.start.isoformat(),
            "event_start_formatted": "Test Date",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "days_until": 14,
        }
        already_sent: set[tuple[t.Any, str]] = set()

        # Act
        service = EventReminderService()
        service.send_ticket_reminders(future_event_14_days, event_context, already_sent)

        # Assert
        call_kwargs = mock_signal.call_args.kwargs
        assert call_kwargs["context"]["ticket_id"] == str(ticket.id)
        assert call_kwargs["context"]["tier_name"] == ticket_tier.name


class TestSendRSVPReminders:
    """Test RSVP reminder sending."""

    @patch("notifications.signals.notification_requested.send")
    def test_sends_reminders_to_rsvp_attendees(
        self,
        mock_signal: MagicMock,
        rsvp_event: Event,
        rsvp_user: RevelUser,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that reminders are sent to RSVP attendees."""
        # Arrange
        EventRSVP.objects.create(
            event=rsvp_event,
            user=rsvp_user,
            status=EventRSVP.RsvpStatus.YES,
        )
        EventRSVP.objects.create(
            event=rsvp_event,
            user=ticket_holder_1,
            status=EventRSVP.RsvpStatus.YES,
        )

        event_context = {
            "event_id": str(rsvp_event.id),
            "event_name": rsvp_event.name,
            "event_start": rsvp_event.start.isoformat(),
            "event_start_formatted": "Test Date",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "days_until": 7,
        }
        already_sent: set[tuple[t.Any, str]] = set()
        sent_to_users: set[t.Any] = set()

        # Act
        service = EventReminderService()
        count = service.send_rsvp_reminders(rsvp_event, event_context, already_sent, sent_to_users)

        # Assert
        assert count == 2
        assert mock_signal.call_count == 2

    @patch("notifications.signals.notification_requested.send")
    def test_includes_rsvp_status_in_context(
        self,
        mock_signal: MagicMock,
        rsvp_event: Event,
        rsvp_user: RevelUser,
    ) -> None:
        """Test that RSVP status is included in context."""
        # Arrange
        EventRSVP.objects.create(
            event=rsvp_event,
            user=rsvp_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        event_context = {
            "event_id": str(rsvp_event.id),
            "event_name": rsvp_event.name,
            "event_start": rsvp_event.start.isoformat(),
            "event_start_formatted": "Test Date",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "days_until": 7,
        }
        already_sent: set[tuple[t.Any, str]] = set()
        sent_to_users: set[t.Any] = set()

        # Act
        service = EventReminderService()
        service.send_rsvp_reminders(rsvp_event, event_context, already_sent, sent_to_users)

        # Assert
        call_kwargs = mock_signal.call_args.kwargs
        assert call_kwargs["context"]["rsvp_status"] == EventRSVP.RsvpStatus.YES


class TestSendEventReminders:
    """Test main event reminder task."""

    @patch("notifications.signals.notification_requested.send")
    @patch("common.models.SiteSettings.get_solo")
    def test_sends_reminders_for_14_day_events(
        self,
        mock_site_settings: MagicMock,
        mock_signal: MagicMock,
        future_event_14_days: Event,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that reminders are sent for events 14 days away."""
        # Arrange
        mock_site_settings.return_value.frontend_base_url = "https://example.com"

        ticket_tier = get_or_create_ticket_tier(future_event_14_days)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_1,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Act
        result = send_event_reminders()

        # Assert
        assert result["reminders_sent"] > 0
        mock_signal.assert_called()

    @patch("notifications.signals.notification_requested.send")
    @patch("common.models.SiteSettings.get_solo")
    def test_prevents_duplicate_reminders(
        self,
        mock_site_settings: MagicMock,
        mock_signal: MagicMock,
        future_event_14_days: Event,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that duplicate reminders are prevented across task runs."""
        # Arrange
        mock_site_settings.return_value.frontend_base_url = "https://example.com"

        ticket_tier = get_or_create_ticket_tier(future_event_14_days)
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event_14_days,
            user=ticket_holder_1,
            tier=ticket_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Create existing reminder
        Notification.objects.create(
            user=ticket_holder_1,
            notification_type=NotificationType.EVENT_REMINDER,
            context={
                "event_id": str(future_event_14_days.id),
                "days_until": 14,
                "ticket_id": str(ticket.id),
            },
        )

        # Act
        result = send_event_reminders()

        # Assert - Should not send duplicate
        assert result["reminders_sent"] == 0
        mock_signal.assert_not_called()

    @patch("notifications.signals.notification_requested.send")
    @patch("common.models.SiteSettings.get_solo")
    def test_only_sends_for_open_events(
        self,
        mock_site_settings: MagicMock,
        mock_signal: MagicMock,
        organization: Organization,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that reminders are only sent for OPEN events."""
        # Arrange
        mock_site_settings.return_value.frontend_base_url = "https://example.com"

        # Create draft event
        draft_event = Event.objects.create(
            organization=organization,
            name="Draft Event",
            slug="draft",
            start=timezone.now() + timedelta(days=14),
            end=timezone.now() + timedelta(days=14, hours=2),
            status=Event.EventStatus.DRAFT,
            visibility=Event.Visibility.PUBLIC,
            requires_ticket=True,
        )

        draft_tier = get_or_create_ticket_tier(draft_event)

        Ticket.objects.create(
            guest_name="Test Guest",
            event=draft_event,
            user=ticket_holder_1,
            tier=draft_tier,
            status=Ticket.TicketStatus.ACTIVE,
        )

        # Act
        result = send_event_reminders()

        # Assert - No reminders for draft events
        assert result["reminders_sent"] == 0
        mock_signal.assert_not_called()

    @patch("notifications.signals.notification_requested.send")
    @patch("common.models.SiteSettings.get_solo")
    def test_sends_reminders_for_multiple_time_windows(
        self,
        mock_site_settings: MagicMock,
        mock_signal: MagicMock,
        organization: Organization,
        ticket_holder_1: RevelUser,
    ) -> None:
        """Test that reminders are sent for 14, 7, and 1 day windows."""
        # Arrange
        mock_site_settings.return_value.frontend_base_url = "https://example.com"

        # Create events at each reminder interval
        events = []
        for days in [14, 7, 1]:
            start = timezone.now() + timedelta(days=days)
            event = Event.objects.create(
                organization=organization,
                name=f"Event in {days} days",
                slug=f"event-{days}",
                start=start,
                end=start + timedelta(hours=2),
                status=Event.EventStatus.OPEN,
                visibility=Event.Visibility.PUBLIC,
                requires_ticket=True,
            )

            tier = get_or_create_ticket_tier(event, name=f"GA-{days}")

            Ticket.objects.create(
                guest_name="Test Guest",
                event=event,
                user=ticket_holder_1,
                tier=tier,
                status=Ticket.TicketStatus.ACTIVE,
            )

            events.append(event)

        # Act
        result = send_event_reminders()

        # Assert - Should send 3 reminders (one for each time window)
        assert result["reminders_sent"] == 3
        assert mock_signal.call_count == 3

    @patch("notifications.signals.notification_requested.send")
    @patch("common.models.SiteSettings.get_solo")
    def test_handles_rsvp_events_separately(
        self,
        mock_site_settings: MagicMock,
        mock_signal: MagicMock,
        rsvp_event: Event,
        rsvp_user: RevelUser,
    ) -> None:
        """Test that RSVP-based events send reminders correctly."""
        # Arrange
        mock_site_settings.return_value.frontend_base_url = "https://example.com"

        EventRSVP.objects.create(
            event=rsvp_event,
            user=rsvp_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Act
        result = send_event_reminders()

        # Assert
        assert result["reminders_sent"] > 0
        mock_signal.assert_called()

        # Verify RSVP context
        call_kwargs = mock_signal.call_args.kwargs
        assert "rsvp_status" in call_kwargs["context"]

    @patch("notifications.signals.notification_requested.send")
    @patch("common.models.SiteSettings.get_solo")
    def test_handles_events_with_no_attendees(
        self,
        mock_site_settings: MagicMock,
        mock_signal: MagicMock,
        future_event_14_days: Event,
    ) -> None:
        """Test that events with no attendees don't cause errors."""
        # Arrange
        mock_site_settings.return_value.frontend_base_url = "https://example.com"

        # Act
        result = send_event_reminders()

        # Assert - Should complete without error
        assert result["reminders_sent"] == 0
        mock_signal.assert_not_called()
