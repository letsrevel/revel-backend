"""Signal handlers for event notifications."""

import typing as t
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Event
from notifications.enums import NotificationType
from notifications.service.eligibility import get_eligible_users_for_event_notification
from notifications.service.notification_helpers import notify_event_opened
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)

# Store previous event state for detecting changes
_event_previous_state: dict[UUID, dict[str, t.Any]] = {}


def _handle_event_cancelled(sender: type[Event], instance: Event) -> None:
    """Handle EVENT_CANCELLED notification."""

    def send_cancellation_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        eligible_users = get_eligible_users_for_event_notification(instance, NotificationType.EVENT_CANCELLED)

        for user in eligible_users:
            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=NotificationType.EVENT_CANCELLED,
                context={
                    "event_id": str(instance.id),
                    "event_name": instance.name,
                    "event_start": instance.start.isoformat() if instance.start else "",
                    "refund_available": instance.requires_ticket,
                    "frontend_url": f"{frontend_base_url}/events/{instance.id}",
                },
            )

        logger.info(
            "event_cancelled_notifications_sent",
            event_id=str(instance.id),
            recipient_count=eligible_users.count(),
        )

    transaction.on_commit(send_cancellation_notifications)


def _handle_event_updated(sender: type[Event], instance: Event, changed_fields: list[str]) -> None:
    """Handle EVENT_UPDATED notification."""

    def send_update_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        eligible_users = get_eligible_users_for_event_notification(instance, NotificationType.EVENT_UPDATED)

        for user in eligible_users:
            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=NotificationType.EVENT_UPDATED,
                context={
                    "event_id": str(instance.id),
                    "event_name": instance.name,
                    "event_start": instance.start.isoformat() if instance.start else "",
                    "changed_fields": ", ".join(changed_fields),
                    "frontend_url": f"{frontend_base_url}/events/{instance.id}",
                },
            )

        logger.info(
            "event_updated_notifications_sent",
            event_id=str(instance.id),
            changed_fields=changed_fields,
            recipient_count=eligible_users.count(),
        )

    transaction.on_commit(send_update_notifications)


def _detect_field_changes(instance: Event, previous_state: dict[str, t.Any]) -> list[str]:
    """Detect which important fields changed.

    Args:
        instance: The current event instance
        previous_state: Dictionary containing previous field values

    Returns:
        List of field names that changed (using display names)
    """
    # Map of instance attribute -> display name in notifications
    watched_fields = {
        "name": "name",
        "start": "start",
        "end": "end",
        "address": "address",
        "city_id": "city",
    }

    changed_fields = []
    for attr_name, display_name in watched_fields.items():
        if previous_state.get(attr_name) != getattr(instance, attr_name):
            changed_fields.append(display_name)

    return changed_fields


@receiver(pre_save, sender=Event)
def capture_event_state(sender: type[Event], instance: Event, **kwargs: t.Any) -> None:
    """Capture event state before save to detect changes."""
    if instance.pk:
        try:
            old_instance = Event.objects.get(pk=instance.pk)
            _event_previous_state[instance.pk] = {
                "status": old_instance.status,
                "name": old_instance.name,
                "start": old_instance.start,
                "end": old_instance.end,
                "address": old_instance.address,
                "city_id": old_instance.city_id,
            }
        except Event.DoesNotExist:
            pass


@receiver(post_save, sender=Event)
def handle_event_notification(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Send notifications for event status changes and updates.

    Handles:
    - EVENT_OPEN: When event is created as OPEN or status changes to OPEN
    - EVENT_CANCELLED: When event status changes to DELETED
    - EVENT_UPDATED: When important fields change
    """
    # Get previous state
    previous_state = _event_previous_state.pop(instance.pk, None) if instance.pk else None

    # Handle EVENT_OPEN
    if instance.status == Event.EventStatus.OPEN:
        update_fields = kwargs.get("update_fields")
        if created or (update_fields and "status" in update_fields):
            transaction.on_commit(lambda: notify_event_opened(instance))

    # Handle EVENT_CANCELLED and EVENT_UPDATED
    if not created and previous_state:
        # Event was cancelled (status changed to DELETED)
        if previous_state["status"] != Event.EventStatus.DELETED and instance.status == Event.EventStatus.DELETED:
            _handle_event_cancelled(sender, instance)

        # Event was updated (important fields changed, not deleted)
        if instance.status != Event.EventStatus.DELETED:
            changed_fields = _detect_field_changes(instance, previous_state)
            if changed_fields:
                _handle_event_updated(sender, instance, changed_fields)
