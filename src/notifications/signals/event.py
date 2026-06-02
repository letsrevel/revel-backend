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
from notifications.service.notification_helpers import (
    _get_event_location_for_user,
    format_event_datetime,
    notify_event_opened,
)
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)

# Store previous event state for detecting changes
_event_previous_state: dict[UUID, dict[str, t.Any]] = {}

# Change-diff display names whose values are gated by the event's address_visibility.
# These must be redacted per-user when the recipient cannot see the address.
_ADDRESS_SENSITIVE_FIELDS = {"address", "city"}


def _handle_event_cancelled(sender: type[Event], instance: Event) -> None:
    """Handle EVENT_CANCELLED notification."""

    def send_cancellation_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        eligible_users = get_eligible_users_for_event_notification(instance, NotificationType.EVENT_CANCELLED)

        # Format event details in event's timezone
        event_start_formatted = format_event_datetime(instance.start, instance)

        # Prepare refund info if tickets are required. ``refund_available`` is a
        # required key on EventCancelledContext, so it must always be present.
        refund_available = instance.requires_ticket
        refund_info = (
            "Refunds will be processed according to the organizer's refund policy." if refund_available else None
        )

        for user in eligible_users:
            # Check address visibility per user
            event_location, address_url = _get_event_location_for_user(instance, user)

            context: dict[str, t.Any] = {
                "event_id": str(instance.id),
                "event_name": instance.name,
                "event_start": instance.start.isoformat() if instance.start else "",
                "event_start_formatted": event_start_formatted,
                "event_location": event_location,
                "event_url": f"{frontend_base_url}/events/{instance.id}",
                "refund_available": refund_available,
            }

            # Add optional fields
            if refund_info:
                context["refund_info"] = refund_info
            if address_url:
                context["address_url"] = address_url

            # Optional fields, only included when set.
            if instance.cancellation_reason:
                context["cancellation_reason"] = instance.cancellation_reason
            # alternative_event_url may be added by admin in a future enhancement
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
    from django.utils.translation import gettext as _

    def send_update_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        eligible_users = get_eligible_users_for_event_notification(instance, NotificationType.EVENT_UPDATED)

        event_start_formatted = format_event_datetime(instance.start, instance)
        event_end_formatted = format_event_datetime(instance.end, instance) or None

        for user in eligible_users:
            # Check address visibility per user
            event_location, address_url = _get_event_location_for_user(instance, user)

            # Redact address/city from the change diff for users who cannot see the address,
            # mirroring the per-user gating already applied to event_location. address_visibility
            # is independent of event visibility, so the EVENT_UPDATED recipient set can be broader
            # than the set allowed to see the venue — building the diff once outside this loop would
            # leak the (old and new) address to recipients who are not permitted to see it.
            if instance.can_user_see_address(user):
                user_changed_fields = changed_fields
                user_old_values = old_values
                user_new_values = new_values
            else:
                user_changed_fields = [f for f in changed_fields if f not in _ADDRESS_SENSITIVE_FIELDS]
                user_old_values = {k: v for k, v in old_values.items() if k not in _ADDRESS_SENSITIVE_FIELDS}
                user_new_values = {k: v for k, v in new_values.items() if k not in _ADDRESS_SENSITIVE_FIELDS}

            # Build the human-readable summary/message from the per-user (redacted) field set.
            changes_summary = ", ".join(user_changed_fields)
            update_parts = [
                f"{field.capitalize()}: {user_old_values.get(field, 'Not set')} → "
                f"{user_new_values.get(field, 'Not set')}"
                for field in user_changed_fields
            ]
            update_message = "; ".join(update_parts) if update_parts else _("Event details have been updated.")

            context = {
                "event_id": str(instance.id),
                "event_name": instance.name,
                "event_start_formatted": event_start_formatted,
                "event_location": event_location,
                "event_url": f"{frontend_base_url}/events/{instance.id}",
                "changed_fields": user_changed_fields,
                "old_values": user_old_values,
                "new_values": user_new_values,
                "changes_summary": changes_summary,
                "update_message": update_message,
            }
            if event_end_formatted:
                context["event_end_formatted"] = event_end_formatted
            if address_url:
                context["address_url"] = address_url

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
                new_values[display_name] = format_event_datetime(new_val, instance)
                if old_val:
                    old_values[display_name] = format_event_datetime(old_val, instance)
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
    from events.suppression import _suppress_event_notifications

    if _suppress_event_notifications.get():
        return

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
            logger.debug("event_not_found_for_old_state", pk=instance.pk)


@receiver(post_save, sender=Event)
def handle_event_notification(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Send notifications for event status changes and updates.

    Handles:
    - EVENT_OPEN: When event is created as OPEN or status changes to OPEN
    - EVENT_CANCELLED: When event status changes to DELETED
    - EVENT_UPDATED: When important fields change
    """
    from events.suppression import _suppress_event_notifications

    if _suppress_event_notifications.get():
        return

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
        if previous_state["status"] != Event.EventStatus.CANCELLED and instance.status == Event.EventStatus.CANCELLED:
            _handle_event_cancelled(sender, instance)

        # Event was updated (important fields changed, not deleted)
        if instance.status != Event.EventStatus.CANCELLED:
            changed_fields, old_values, new_values = _detect_field_changes(instance, previous_state)
            if changed_fields:
                _handle_event_updated(sender, instance, changed_fields, old_values, new_values)
