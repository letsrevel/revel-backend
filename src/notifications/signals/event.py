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
    from django.utils.dateformat import format as date_format

    def send_cancellation_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        eligible_users = get_eligible_users_for_event_notification(instance, NotificationType.EVENT_CANCELLED)

        # Format event details
        event_start_formatted = date_format(instance.start, "l, F j, Y \\a\\t g:i A T") if instance.start else ""
        event_location = instance.full_address()

        # Prepare refund info if tickets are required
        refund_info = None
        if instance.requires_ticket:
            refund_info = "Refunds will be processed according to the organizer's refund policy."

        for user in eligible_users:
            context = {
                "event_id": str(instance.id),
                "event_name": instance.name,
                "event_start": instance.start.isoformat() if instance.start else "",
                "event_start_formatted": event_start_formatted,
                "event_location": event_location,
                "event_url": f"{frontend_base_url}/events/{instance.id}",
            }

            # Add optional fields
            if refund_info:
                context["refund_info"] = refund_info

            # These fields could be added by admin in future enhancements
            # For now, they're optional and only included if set
            if hasattr(instance, "cancellation_reason") and instance.cancellation_reason:
                context["cancellation_reason"] = instance.cancellation_reason
            if hasattr(instance, "alternative_event_url") and instance.alternative_event_url:
                context["alternative_event_url"] = instance.alternative_event_url

            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=NotificationType.EVENT_CANCELLED,
                context=context,
            )

        logger.info(
            "event_cancelled_notifications_sent",
            event_id=str(instance.id),
            recipient_count=eligible_users.count(),
        )

    transaction.on_commit(send_cancellation_notifications)


def _handle_event_updated(
    sender: type[Event],
    instance: Event,
    changed_fields: list[str],
    old_values: dict[str, str],
    new_values: dict[str, str],
) -> None:
    """Handle EVENT_UPDATED notification."""
    from django.utils.dateformat import format as date_format
    from django.utils.translation import gettext as _

    def send_update_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        eligible_users = get_eligible_users_for_event_notification(instance, NotificationType.EVENT_UPDATED)

        event_start_formatted = date_format(instance.start, "l, F j, Y \\a\\t g:i A T") if instance.start else ""
        event_end_formatted = date_format(instance.end, "l, F j, Y \\a\\t g:i A T") if instance.end else None
        event_location = instance.full_address()

        # Build human-readable summary and message
        changes_summary = ", ".join(changed_fields)

        # Build a detailed update message
        update_parts = []
        for field in changed_fields:
            old_val = old_values.get(field, "Not set")
            new_val = new_values.get(field, "Not set")
            update_parts.append(f"{field.capitalize()}: {old_val} → {new_val}")

        update_message = "; ".join(update_parts) if update_parts else _("Event details have been updated.")

        for user in eligible_users:
            context = {
                "event_id": str(instance.id),
                "event_name": instance.name,
                "event_start_formatted": event_start_formatted,
                "event_location": event_location,
                "event_url": f"{frontend_base_url}/events/{instance.id}",
                "changed_fields": changed_fields,
                "old_values": old_values,
                "new_values": new_values,
                "changes_summary": changes_summary,
                "update_message": update_message,
            }
            if event_end_formatted:
                context["event_end_formatted"] = event_end_formatted

            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=NotificationType.EVENT_UPDATED,
                context=context,
            )

        logger.info(
            "event_updated_notifications_sent",
            event_id=str(instance.id),
            changed_fields=changed_fields,
            recipient_count=eligible_users.count(),
        )

    transaction.on_commit(send_update_notifications)


def _detect_field_changes(
    instance: Event, previous_state: dict[str, t.Any]
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Detect which important fields changed.

    Args:
        instance: The current event instance
        previous_state: Dictionary containing previous field values

    Returns:
        Tuple of (changed_fields list, old_values dict, new_values dict)
    """
    from django.utils.dateformat import format as date_format

    # Map of instance attribute -> display name in notifications
    watched_fields = {
        "name": "name",
        "start": "start",
        "end": "end",
        "address": "address",
        "city_id": "city",
    }

    changed_fields = []
    old_values = {}
    new_values = {}

    for attr_name, display_name in watched_fields.items():
        old_val = previous_state.get(attr_name)
        new_val = getattr(instance, attr_name)

        if old_val != new_val:
            changed_fields.append(display_name)

            # Format values for display
            if attr_name in ("start", "end") and new_val:
                new_values[display_name] = date_format(new_val, "l, F j, Y \\a\\t g:i A T")
                if old_val:
                    old_values[display_name] = date_format(old_val, "l, F j, Y \\a\\t g:i A T")
                else:
                    old_values[display_name] = "Not set"
            elif attr_name == "city_id":
                new_values[display_name] = instance.city.name if instance.city else "Not set"
                if old_val and instance.city:
                    # We don't have the old city object, so we can't get the name
                    old_values[display_name] = "Changed"
                else:
                    old_values[display_name] = "Not set"
            else:
                new_values[display_name] = str(new_val) if new_val else "Not set"
                old_values[display_name] = str(old_val) if old_val else "Not set"

    return changed_fields, old_values, new_values


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
            changed_fields, old_values, new_values = _detect_field_changes(instance, previous_state)
            if changed_fields:
                _handle_event_updated(sender, instance, changed_fields, old_values, new_values)
