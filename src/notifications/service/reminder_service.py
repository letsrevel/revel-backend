"""Service for sending event reminder notifications."""

import typing as t
from datetime import timedelta
from uuid import UUID

import structlog
from django.db.models import Prefetch, QuerySet
from django.utils import timezone
from django.utils.dateformat import format as date_format

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, EventRSVP, Ticket
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


class EventReminderService:
    """Service for managing event reminder notifications.

    This service handles the complete workflow of sending event reminders:
    - Querying events happening at specific intervals
    - Building notification contexts
    - Deduplication of reminders
    - Sending reminders to ticket holders and RSVP users
    """

    def __init__(self, reminder_days: list[int] | None = None, frontend_base_url: str | None = None):
        """Initialize the reminder service.

        Args:
            reminder_days: Days before event to send reminders (default: [14, 7, 1])
            frontend_base_url: Frontend base URL (fetched from settings if not provided)
        """
        self.reminder_days = reminder_days or [14, 7, 1]
        self.frontend_base_url = frontend_base_url or SiteSettings.get_solo().frontend_base_url

    def get_events_for_reminder(self, days: int) -> QuerySet[Event]:
        """Get events that start in exactly N days.

        Args:
            days: Number of days until event

        Returns:
            QuerySet of events with prefetched tickets and RSVPs
        """
        now = timezone.now()
        target_date = now + timedelta(days=days)
        date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        date_end = date_start + timedelta(days=1)

        return (
            Event.objects.filter(start__gte=date_start, start__lt=date_end, status=Event.EventStatus.OPEN)
            .select_related("organization", "city")
            .prefetch_related(
                Prefetch(
                    "tickets",
                    queryset=Ticket.objects.filter(
                        status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING]
                    ).select_related("user", "user__notification_preferences", "tier"),
                ),
                Prefetch(
                    "rsvps",
                    queryset=EventRSVP.objects.filter(status=EventRSVP.RsvpStatus.YES).select_related(
                        "user", "user__notification_preferences"
                    ),
                ),
            )
        )

    def get_already_sent_reminders(self, event_ids: list[str], days: int) -> set[tuple[UUID, str]]:
        """Get set of (user_id, event_id) for already-sent reminders.

        Args:
            event_ids: List of event IDs to check
            days: Days until event

        Returns:
            Set of (user_id, event_id) tuples for sent reminders
        """
        existing_reminders = (
            Notification.objects.filter(
                notification_type=NotificationType.EVENT_REMINDER,
                context__event_id__in=event_ids,
                context__days_until=days,
            )
            .values_list("user_id", "context__event_id")
            .distinct()
        )
        return {(user_id, event_id) for user_id, event_id in existing_reminders}

    def build_event_context(self, event: Event, days: int) -> dict[str, t.Any]:
        """Build base context dictionary for event reminder.

        Args:
            event: Event to build context for
            days: Days until event

        Returns:
            Context dictionary for notification templates
        """
        event_url = f"{self.frontend_base_url}/events/{event.id}"
        event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T")
        event_end_formatted = date_format(event.end, "l, F j, Y \\a\\t g:i A T") if event.end else None
        event_location = event.full_address()

        context: dict[str, t.Any] = {
            "event_id": str(event.id),
            "event_name": event.name,
            "event_start": event.start.isoformat(),
            "event_start_formatted": event_start_formatted,
            "event_location": event_location,
            "event_url": event_url,
            "days_until": days,
        }

        if event_end_formatted:
            context["event_end_formatted"] = event_end_formatted

        return context

    def should_send_reminder(self, user: RevelUser, event_id: str, already_sent: set[tuple[UUID, str]]) -> bool:
        """Check if reminder should be sent to user.

        Args:
            user: User to check
            event_id: Event ID
            already_sent: Set of already-sent (user_id, event_id) tuples

        Returns:
            True if reminder should be sent
        """
        prefs = user.notification_preferences
        if not prefs.event_reminders_enabled:
            return False

        if not prefs.is_notification_type_enabled(NotificationType.EVENT_REMINDER):
            return False

        if (user.id, event_id) in already_sent:
            return False

        return True

    def send_ticket_reminders(
        self, event: Event, base_context: dict[str, t.Any], already_sent: set[tuple[UUID, str]]
    ) -> tuple[int, set[UUID]]:
        """Send reminders to all ticket holders for an event.

        Args:
            event: Event to send reminders for
            base_context: Base context dictionary
            already_sent: Set of already-sent (user_id, event_id) tuples

        Returns:
            Tuple of (count sent, set of user IDs sent to)
        """
        count = 0
        sent_to_users: set[UUID] = set()
        event_id_str = str(event.id)

        for ticket in event.tickets.all():
            user = ticket.user
            if user.id in sent_to_users or not self.should_send_reminder(user, event_id_str, already_sent):
                continue

            context = {**base_context, "ticket_id": str(ticket.id), "tier_name": ticket.tier.name}

            notification_requested.send(
                sender=Ticket,
                user=user,
                notification_type=NotificationType.EVENT_REMINDER,
                context=context,
            )
            sent_to_users.add(user.id)
            count += 1

        return count, sent_to_users

    def send_rsvp_reminders(
        self,
        event: Event,
        base_context: dict[str, t.Any],
        already_sent: set[tuple[UUID, str]],
        sent_to_users: set[UUID],
    ) -> int:
        """Send reminders to users who RSVP'd YES but don't have tickets.

        Args:
            event: Event to send reminders for
            base_context: Base context dictionary
            already_sent: Set of already-sent (user_id, event_id) tuples
            sent_to_users: Set of user IDs already sent to (will not send again)

        Returns:
            Count of reminders sent
        """
        count = 0
        event_id_str = str(event.id)

        for rsvp in event.rsvps.all():
            user = rsvp.user
            if user.id in sent_to_users or not self.should_send_reminder(user, event_id_str, already_sent):
                continue

            context = {**base_context, "rsvp_status": rsvp.status}

            notification_requested.send(
                sender=EventRSVP,
                user=user,
                notification_type=NotificationType.EVENT_REMINDER,
                context=context,
            )
            sent_to_users.add(user.id)
            count += 1

        return count

    def send_all_reminders(self) -> dict[str, t.Any]:
        """Send reminders for all upcoming events.

        Returns:
            Statistics dictionary with reminder count
        """
        reminders_sent = 0

        for days in self.reminder_days:
            events = list(self.get_events_for_reminder(days))

            logger.info(
                "event_reminder_scan",
                days_until=days,
                events_found=len(events),
            )

            event_ids = [str(e.id) for e in events]
            already_sent = self.get_already_sent_reminders(event_ids, days)

            for event in events:
                event_context = self.build_event_context(event, days)

                # Send to ticket holders
                ticket_count, sent_to_users = self.send_ticket_reminders(event, event_context, already_sent)
                reminders_sent += ticket_count

                # Send to RSVP users (if event doesn't require tickets)
                if not event.requires_ticket:
                    rsvp_count = self.send_rsvp_reminders(event, event_context, already_sent, sent_to_users)
                    reminders_sent += rsvp_count

        logger.info("event_reminders_sent", count=reminders_sent)
        return {"reminders_sent": reminders_sent}
