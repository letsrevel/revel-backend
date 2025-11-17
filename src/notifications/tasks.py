"""Celery tasks for notification dispatch and maintenance."""

import typing as t
from datetime import timedelta

import structlog
from celery import group, shared_task
from django.conf import settings
from django.utils import timezone, translation

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery, NotificationPreference
from notifications.service.channels.registry import get_channel_instance

logger = structlog.get_logger(__name__)


@shared_task(bind=True, max_retries=3)
def dispatch_notification(self: t.Any, notification_id: str) -> dict[str, t.Any]:
    """Main dispatcher task - creates delivery records and dispatches to channels.

    This task:
    1. Loads notification
    2. Renders title/body from template (using recipient's language)
    3. Determines delivery channels based on user preferences
    4. Creates NotificationDelivery records
    5. Dispatches to channel-specific delivery tasks

    Args:
        self: Celery task instance (automatically passed when bind=True)
        notification_id: UUID of notification to dispatch

    Returns:
        Dict with dispatch stats
    """
    notification = Notification.objects.select_related("user", "user__notification_preferences").get(pk=notification_id)

    # Get recipient's language preference
    user_language = getattr(notification.user, "language", settings.LANGUAGE_CODE)

    # Render title and body from template
    # CRITICAL: Activate recipient's language, not sender's or system default
    try:
        from notifications.service.templates.registry import get_template

        template = get_template(notification.notification_type)

        # Activate user's language for rendering
        with translation.override(user_language):
            notification.title = template.get_in_app_title(notification)
            notification.body = template.get_in_app_body(notification)

        notification.save(update_fields=["title", "body", "updated_at"])

        logger.debug(
            "notification_rendered",
            notification_id=notification_id,
            notification_type=notification.notification_type,
            user_language=user_language,
        )
    except Exception as e:
        logger.error(
            "notification_render_failed",
            notification_id=notification_id,
            notification_type=notification.notification_type,
            error=str(e),
        )
        # Continue even if rendering fails - channels can use context directly

    # Determine delivery channels
    from notifications.service.dispatcher import determine_delivery_channels

    channels = determine_delivery_channels(notification.user, notification.notification_type)

    # Create delivery records
    deliveries = []
    for channel in channels:
        delivery, created = NotificationDelivery.objects.get_or_create(
            notification=notification,
            channel=channel,
            defaults={"status": DeliveryStatus.PENDING},
        )
        if created:
            deliveries.append(delivery)

    # Dispatch to channels in parallel
    if deliveries:
        delivery_tasks = group(deliver_to_channel.si(str(delivery.id)) for delivery in deliveries)
        delivery_tasks.apply_async()

    logger.info(
        "notification_dispatched",
        notification_id=notification_id,
        notification_type=notification.notification_type,
        user_id=str(notification.user.id),
        channels=channels,
        delivery_count=len(deliveries),
    )

    return {
        "notification_id": notification_id,
        "channels": channels,
        "deliveries_created": len(deliveries),
    }


@shared_task(bind=True, max_retries=3)
def deliver_to_channel(self: t.Any, delivery_id: str) -> dict[str, t.Any]:
    """Deliver notification through specific channel.

    Handles retries for transient failures.

    Args:
        self: Celery task instance (automatically passed when bind=True)
        delivery_id: UUID of delivery record

    Returns:
        Dict with delivery result
    """
    delivery = NotificationDelivery.objects.select_related("notification", "notification__user").get(pk=delivery_id)

    # Get channel instance
    channel = get_channel_instance(delivery.channel)

    # Check if delivery should proceed
    if not channel.can_deliver(delivery.notification):
        delivery.status = DeliveryStatus.SKIPPED
        delivery.save(update_fields=["status", "updated_at"])
        logger.info(
            "delivery_skipped",
            delivery_id=delivery_id,
            channel=delivery.channel,
            reason="user_preferences",
        )
        return {"status": "skipped", "reason": "user_preferences"}

    # Attempt delivery
    try:
        success = channel.deliver(delivery.notification, delivery)

        if not success:
            logger.warning(
                "delivery_failed_gracefully",
                delivery_id=delivery_id,
                channel=delivery.channel,
            )
            return {"status": "failed", "graceful": True}

        return {"status": "sent", "channel": delivery.channel}

    except Exception as e:
        # Refresh delivery object to get updated retry_count from channel's deliver() method
        delivery.refresh_from_db()

        logger.error(
            "delivery_exception",
            delivery_id=delivery_id,
            channel=delivery.channel,
            error=str(e),
            retry_count=delivery.retry_count,
        )

        # Check if we should retry
        if channel.should_retry(e) and delivery.retry_count < 3:
            # Exponential backoff: 2^retry_count minutes
            countdown = 2**delivery.retry_count * 60
            logger.info(
                "retrying_delivery",
                delivery_id=delivery_id,
                channel=delivery.channel,
                countdown=countdown,
                retry_count=delivery.retry_count,
            )
            raise self.retry(exc=e, countdown=countdown)
        else:
            # Don't retry - permanent failure
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = str(e)
            delivery.save(update_fields=["status", "error_message", "updated_at"])
            raise


# ===== Digest Tasks =====


@shared_task
def send_notification_digests() -> dict[str, t.Any]:
    """Send notification digests to users based on their preferences.

    Runs every hour via Celery beat.
    Checks which users should receive digests now and sends them.

    Returns:
        Dict with digest stats
    """
    from notifications.service.digest import (
        NotificationDigest,
        get_digest_lookback_period,
        get_pending_notifications_for_digest,
        should_send_digest_now,
    )

    # Get users who want digests (not immediate)
    users_with_digests = NotificationPreference.objects.exclude(
        digest_frequency=NotificationPreference.DigestFrequency.IMMEDIATE
    ).select_related("user")

    digests_sent = 0
    digests_skipped = 0

    for prefs in users_with_digests:
        # Check if it's time to send digest for this user
        if not should_send_digest_now(prefs.user):
            digests_skipped += 1
            continue

        # Get lookback period
        lookback = get_digest_lookback_period(prefs.digest_frequency)
        since = timezone.now() - lookback

        # Get pending notifications
        pending = get_pending_notifications_for_digest(prefs.user, since)

        if not pending.exists():
            continue  # No notifications to send

        # Build and send digest
        digest = NotificationDigest(prefs.user, pending)
        success = digest.send_digest_email()

        if success:
            # Mark notifications as having email delivery
            for notification in pending:
                delivery, _ = NotificationDelivery.objects.get_or_create(
                    notification=notification,
                    channel=DeliveryChannel.EMAIL,
                    defaults={
                        "status": DeliveryStatus.SENT,
                        "delivered_at": timezone.now(),
                        "metadata": {"digest": True},
                    },
                )

            digests_sent += 1

    logger.info("digests_sent", count=digests_sent, skipped=digests_skipped)

    return {"digests_sent": digests_sent, "digests_skipped": digests_skipped}


# ===== Maintenance Tasks =====


@shared_task
def cleanup_old_notifications() -> dict[str, t.Any]:
    """Clean up notifications older than configured retention period.

    Runs daily via Celery beat.
    Retention period is configured via NOTIFICATION_RETENTION_DAYS env var (default 90).

    Returns:
        Dict with cleanup stats
    """
    retention_days = getattr(settings, "NOTIFICATION_RETENTION_DAYS", 90)
    cutoff = timezone.now() - timedelta(days=retention_days)

    # Delete old notifications (cascades to deliveries)
    deleted_count, _ = Notification.objects.filter(created_at__lt=cutoff).delete()

    logger.info("notifications_cleaned_up", retention_days=retention_days, deleted_count=deleted_count)

    return {"deleted_count": deleted_count, "retention_days": retention_days}


@shared_task
def retry_failed_deliveries() -> dict[str, t.Any]:
    """Retry failed deliveries that might be recoverable.

    Runs every 6 hours via Celery beat.
    Only retries failures from last 24 hours with retry_count < 5.

    Returns:
        Dict with retry stats
    """
    twenty_four_hours_ago = timezone.now() - timedelta(hours=24)

    failed_deliveries = NotificationDelivery.objects.filter(
        status=DeliveryStatus.FAILED, retry_count__lt=5, created_at__gte=twenty_four_hours_ago
    ).select_related("notification")

    retry_count = 0

    for delivery in failed_deliveries:
        # Reset status to pending
        delivery.status = DeliveryStatus.PENDING
        delivery.save(update_fields=["status", "updated_at"])

        # Re-dispatch
        deliver_to_channel.delay(str(delivery.id))
        retry_count += 1

    logger.info("failed_deliveries_retried", count=retry_count)

    return {"retried_count": retry_count}


def _should_send_reminder(user: t.Any, event_id: str, already_sent: set[tuple[t.Any, str]]) -> bool:
    """Check if reminder should be sent to user."""
    from notifications.enums import NotificationType

    prefs = user.notification_preferences
    if not prefs.event_reminders_enabled:
        return False

    if not prefs.is_notification_type_enabled(NotificationType.EVENT_REMINDER):
        return False

    if (user.id, event_id) in already_sent:
        return False

    return True


def _build_event_context(
    event: t.Any,
    event_url: str,
    event_start_formatted: str,
    event_end_formatted: str | None,
    event_location: str,
    days: int,
) -> dict[str, t.Any]:
    """Build base event context for reminder."""
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


def _send_ticket_reminders(
    event: t.Any, event_context: dict[str, t.Any], already_sent: set[tuple[t.Any, str]], sent_to_users: set[t.Any]
) -> int:
    """Send reminders to ticket holders."""
    from notifications.enums import NotificationType
    from notifications.signals import notification_requested

    count = 0
    event_id_str = str(event.id)

    for ticket in event.tickets.all():
        user = ticket.user
        if user.id in sent_to_users or not _should_send_reminder(user, event_id_str, already_sent):
            continue

        context = {**event_context, "ticket_id": str(ticket.id), "tier_name": ticket.tier.name}

        notification_requested.send(
            sender="send_event_reminders", user=user, notification_type=NotificationType.EVENT_REMINDER, context=context
        )

        count += 1
        sent_to_users.add(user.id)

    return count


def _send_rsvp_reminders(
    event: t.Any, event_context: dict[str, t.Any], already_sent: set[tuple[t.Any, str]], sent_to_users: set[t.Any]
) -> int:
    """Send reminders to RSVP attendees."""
    from notifications.enums import NotificationType
    from notifications.signals import notification_requested

    count = 0
    event_id_str = str(event.id)

    for rsvp in event.rsvps.all():
        user = rsvp.user
        if user.id in sent_to_users or not _should_send_reminder(user, event_id_str, already_sent):
            continue

        context = {**event_context, "rsvp_status": rsvp.status}

        notification_requested.send(
            sender="send_event_reminders", user=user, notification_type=NotificationType.EVENT_REMINDER, context=context
        )

        count += 1
        sent_to_users.add(user.id)

    return count


@shared_task
def send_event_reminders() -> dict[str, t.Any]:
    """Send event reminders for upcoming events.

    Runs daily via Celery beat.
    Sends reminders at 14, 7, and 1 days before events.
    Ensures each reminder is sent only once per user per event.

    Returns:
        Dict with reminder stats
    """
    from django.db.models import Prefetch
    from django.utils.dateformat import format as date_format

    from common.models import SiteSettings
    from events.models import Event, EventRSVP, Ticket
    from notifications.enums import NotificationType
    from notifications.models import Notification

    now = timezone.now()
    reminder_days = [14, 7, 1]
    reminders_sent = 0
    frontend_base_url = SiteSettings.get_solo().frontend_base_url

    for days in reminder_days:
        target_date = now + timedelta(days=days)
        date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        date_end = date_start + timedelta(days=1)

        events = (
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

        logger.info(
            "event_reminder_scan",
            days_until=days,
            date_start=date_start.isoformat(),
            date_end=date_end.isoformat(),
            events_found=events.count(),
        )

        event_ids = [str(e.id) for e in events]
        existing_reminders = (
            Notification.objects.filter(
                notification_type=NotificationType.EVENT_REMINDER,
                context__event_id__in=event_ids,
                context__days_until=days,
            )
            .values_list("user_id", "context__event_id")
            .distinct()
        )
        already_sent = {(user_id, event_id) for user_id, event_id in existing_reminders}

        for event in events:
            event_url = f"{frontend_base_url}/events/{event.id}"
            event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T")
            event_end_formatted = date_format(event.end, "l, F j, Y \\a\\t g:i A T") if event.end else None
            event_location = event.address or (event.city.name if event.city else "")

            event_context = _build_event_context(
                event, event_url, event_start_formatted, event_end_formatted, event_location, days
            )
            sent_to_users: set[t.Any] = set()

            reminders_sent += _send_ticket_reminders(event, event_context, already_sent, sent_to_users)

            if not event.requires_ticket:
                reminders_sent += _send_rsvp_reminders(event, event_context, already_sent, sent_to_users)

    logger.info("event_reminders_sent", count=reminders_sent)

    return {"reminders_sent": reminders_sent}
