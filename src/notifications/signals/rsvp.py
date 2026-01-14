"""Signal handlers for rsvp notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from events.models import EventRSVP
from events.tasks import build_attendee_visibility_flags
from notifications.enums import NotificationType
from notifications.service.eligibility import get_organization_staff_and_owners
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def _build_rsvp_context(rsvp: EventRSVP) -> dict[str, t.Any]:
    """Build notification context for RSVP."""
    from django.utils.dateformat import format as date_format

    from common.models import SiteSettings

    event = rsvp.event
    frontend_base_url = SiteSettings.get_solo().frontend_base_url

    event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
    event_location = event.full_address()

    context = {
        "rsvp_id": str(rsvp.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start": event.start.isoformat() if event.start else "",
        "event_start_formatted": event_start_formatted,
        "event_location": event_location,
        "event_url": f"{frontend_base_url}/events/{event.id}",
        "response": rsvp.status,
        "user_name": rsvp.user.get_display_name(),
        "user_email": rsvp.user.email,
    }

    # Add optional fields if available in the model
    if hasattr(rsvp, "guest_count"):
        context["guest_count"] = rsvp.guest_count
    if hasattr(rsvp, "dietary_restrictions") and rsvp.dietary_restrictions:
        context["dietary_restrictions"] = rsvp.dietary_restrictions

    return context


def _notify_staff_about_rsvp(rsvp: EventRSVP, notification_type: str, context: dict[str, t.Any]) -> None:
    """Notify staff/owners about RSVP event."""
    staff_and_owners = get_organization_staff_and_owners(rsvp.event.organization_id)
    for recipient in staff_and_owners:
        notification_requested.send(
            sender=EventRSVP,
            user=recipient,
            notification_type=notification_type,
            context=context,
        )


def _send_rsvp_confirmation_notifications(rsvp: EventRSVP) -> None:
    """Send notifications when RSVP is created."""
    context = _build_rsvp_context(rsvp)
    _notify_staff_about_rsvp(rsvp, NotificationType.RSVP_CONFIRMATION, context)


def _send_rsvp_updated_notifications(rsvp: EventRSVP) -> None:
    """Send notifications when RSVP is updated."""
    from django.utils.dateformat import format as date_format

    from common.models import SiteSettings

    # Check if old status was captured in pre_save
    if not hasattr(rsvp, "_old_status"):
        return  # No status change

    old_status = rsvp._old_status

    # Skip if old and new status are the same
    if old_status == rsvp.status:
        return

    event = rsvp.event
    frontend_base_url = SiteSettings.get_solo().frontend_base_url

    event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
    event_location = event.full_address()

    context = {
        "rsvp_id": str(rsvp.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": event_start_formatted,
        "event_location": event_location,
        "event_url": f"{frontend_base_url}/events/{event.id}",
        "old_response": old_status,
        "new_response": rsvp.status,
        "user_name": rsvp.user.get_display_name(),
        "user_email": rsvp.user.email,
    }

    # Add optional fields if available
    if hasattr(rsvp, "guest_count"):
        context["guest_count"] = rsvp.guest_count

    _notify_staff_about_rsvp(rsvp, NotificationType.RSVP_UPDATED, context)


@receiver(pre_save, sender=EventRSVP)
def capture_rsvp_old_status(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save."""
    if instance.pk:
        try:
            old_instance = EventRSVP.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except EventRSVP.DoesNotExist:
            logger.debug("rsvp_not_found_for_old_status", pk=instance.pk)


@receiver(post_save, sender=EventRSVP)
def handle_event_rsvp_save(sender: type[EventRSVP], instance: EventRSVP, created: bool, **kwargs: t.Any) -> None:
    """Send notifications after RSVP is changed or created.

    Sends notifications to:
    - Organization staff and owners (NOT the user who RSVPed)
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))

    def send_notifications() -> None:
        if created:
            _send_rsvp_confirmation_notifications(instance)
        else:
            _send_rsvp_updated_notifications(instance)

    transaction.on_commit(send_notifications)


@receiver(post_delete, sender=EventRSVP)
def handle_event_rsvp_delete(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Send notifications after RSVP is deleted.

    Sends notifications to:
    - Organization staff and owners (the user already knows they cancelled)
    """
    from django.utils.dateformat import format as date_format

    build_attendee_visibility_flags.delay(str(instance.event_id))

    # Send notifications after transaction commits
    def send_notifications() -> None:
        from common.models import SiteSettings

        event = instance.event
        user = instance.user
        frontend_base_url = SiteSettings.get_solo().frontend_base_url

        event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
        event_location = event.full_address()

        notification_type = NotificationType.RSVP_CANCELLED
        context = {
            "event_id": str(event.id),
            "event_name": event.name,
            "event_start_formatted": event_start_formatted,
            "event_location": event_location,
            "event_url": f"{frontend_base_url}/events/{event.id}",
            "user_name": user.display_name,
        }

        # Add optional cancellation_reason if available
        if hasattr(instance, "cancellation_reason") and instance.cancellation_reason:
            context["cancellation_reason"] = instance.cancellation_reason

        # Notify organization staff and owners only (user already knows they cancelled)
        staff_and_owners = get_organization_staff_and_owners(event.organization_id)
        for recipient in staff_and_owners:
            notification_requested.send(
                sender=sender,
                user=recipient,
                notification_type=notification_type,
                context=context,
            )

    transaction.on_commit(send_notifications)
