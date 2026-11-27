"""Receivers for Event: the default tier, the status capture and the follower notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from events.models import (
    DEFAULT_TICKET_TIER_NAME,
    Event,
    TicketTier,
)
from events.service import (
    day_membership_service,
)
from events.service.follow_service import get_followers_for_new_event_notification
from events.utils import format_event_datetime
from events.utils.frontend_urls import frontend_base_url_for
from notifications.enums import NotificationType
from notifications.service.eligibility import BatchParticipationChecker
from notifications.signals import notification_requested

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("events.signals")


@receiver(post_save, sender=Event)
def handle_event_save(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Handle event creation and updates."""
    from events.suppression import is_default_tier_creation_suppressed

    if is_default_tier_creation_suppressed():
        return

    # Create default ticket tier if needed
    if instance.requires_ticket and not TicketTier.objects.filter(event=instance).exists():
        TicketTier.objects.create(event=instance, name=DEFAULT_TICKET_TIER_NAME)


@receiver(pre_delete, sender=Event)
def revoke_day_memberships_on_event_delete(sender: type[Event], instance: Event, **kwargs: t.Any) -> None:
    """Deleting an event that has not started leaves no live future day memberships (records survive, #1320)."""
    day_membership_service.revoke_for_deleted_event(instance)


@receiver(pre_save, sender=Event)
def capture_event_old_status(sender: type[Event], instance: Event, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save.

    This allows us to reliably detect when an event's status changes to OPEN,
    regardless of whether save() is called with or without update_fields.
    """
    from events.suppression import is_event_notifications_suppressed

    if is_event_notifications_suppressed():
        return

    if instance.pk:
        try:
            old_instance = Event.objects.only("status").get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except Event.DoesNotExist:
            # Event was deleted between check and fetch (race condition) - skip silently
            pass


def _should_notify_followers_for_event(event: Event, created: bool) -> bool:
    """Check if followers should be notified for this event status change.

    Returns True only when a PUBLIC event transitions to OPEN status (either on
    creation or via update). Non-public events are only visible to members, staff,
    or explicitly invited users — not general followers.
    """
    if event.status != Event.EventStatus.OPEN:
        return False

    # Only PUBLIC events notify followers. UNLISTED, PRIVATE, MEMBERS_ONLY,
    # STAFF_ONLY do not — the org decides explicitly who to share with.
    if event.visibility != Event.Visibility.PUBLIC:
        return False

    if created:
        return True

    # For existing events, check if status actually changed to OPEN
    old_status = getattr(event, "_old_status", None)
    return old_status is not None and old_status != Event.EventStatus.OPEN


@receiver(post_save, sender=Event)
def handle_event_opened_notify_followers(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Notify followers when an event becomes OPEN.

    Sends notifications to:
    - Organization followers who have notify_new_events enabled
    - Event series followers (if event belongs to a series) who have notify_new_events enabled

    Series followers are prioritized - if a user follows both the org and series,
    they receive the series notification only.
    """
    from events.suppression import is_event_notifications_suppressed

    if is_event_notifications_suppressed():
        return

    if not _should_notify_followers_for_event(instance, created):
        return

    organization = instance.organization
    if not organization:
        return

    def send_follower_notifications() -> None:
        frontend_base_url = frontend_base_url_for(organization)
        event_series = instance.event_series
        # One batched visibility checker for all followers (no per-follower queries)
        checker = BatchParticipationChecker(instance)
        full_address = instance.full_address()

        for user, notification_type in get_followers_for_new_event_notification(organization, event_series):
            # Address (and city) only for followers allowed to see it
            visible_location = full_address if checker.can_see_address(user.id) else ""
            context: dict[str, t.Any] = {
                "organization_id": str(organization.id),
                "organization_name": organization.name,
                "event_id": str(instance.id),
                "event_name": instance.name,
                "event_description": instance.description or "",
                "event_start": instance.start.isoformat() if instance.start else "",
                "event_start_formatted": format_event_datetime(instance.start, instance),
                "event_location": visible_location,
                "event_url": f"{frontend_base_url}/events/{instance.id}",
            }

            if notification_type == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES and event_series:
                context["event_series_id"] = str(event_series.id)
                context["event_series_name"] = event_series.name

            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=notification_type,
                context=context,
            )

        logger.info(
            "follower_notifications_sent_for_event",
            event_id=str(instance.id),
            organization_id=str(organization.id),
        )

    transaction.on_commit(send_follower_notifications)
