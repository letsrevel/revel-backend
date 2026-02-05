"""Signal handlers for waitlist notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Event, EventRSVP, EventWaitList, Ticket
from notifications.enums import NotificationType
from notifications.service.notification_helpers import format_event_datetime
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def _calculate_attendee_count(event: Event) -> int:
    """Calculate current attendee count for an event.

    This matches the business logic in EventManager._assert_capacity:
    - For ticket events: count ALL non-cancelled tickets (PENDING tickets reserve a spot)
    - For RSVP events: count only YES RSVPs

    Args:
        event: Event to calculate count for

    Returns:
        Current number of attendees (or reserved spots)
    """
    if event.requires_ticket:
        # Count unique ticket holders (exclude CANCELLED tickets)
        return (
            Ticket.objects.filter(event=event)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .values("user_id")
            .distinct()
            .count()
        )
    # Count YES RSVPs
    return EventRSVP.objects.filter(event=event, status=EventRSVP.RsvpStatus.YES).count()


def _build_waitlist_spot_context(event: Event, spots_available: int) -> dict[str, t.Any]:
    """Build notification context for WAITLIST_SPOT_AVAILABLE.

    Args:
        event: Event with available spots
        spots_available: Number of spots that became available

    Returns:
        Context dictionary for notification
    """
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    event_location = event.full_address()
    event_start_formatted = format_event_datetime(event.start, event)

    return {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start": event.start.isoformat() if event.start else "",
        "event_start_formatted": event_start_formatted,
        "event_location": event_location,
        "event_url": f"{frontend_base_url}/events/{event.id}",
        "organization_id": str(event.organization_id),
        "organization_name": event.organization.name,
        "spots_available": spots_available,
    }


def _notify_waitlist(event: Event, spots_available: int) -> None:
    """Send spot available notifications to all users on the waitlist.

    Args:
        event: Event with available spots
        spots_available: Number of spots that became available
    """
    context = _build_waitlist_spot_context(event, spots_available)

    # Get all waitlisted users ordered by join time (FIFO)
    waitlisted_users = EventWaitList.objects.filter(event=event).select_related("user").order_by("created_at")

    for waitlist_entry in waitlisted_users:
        notification_requested.send(
            sender=EventWaitList,
            user=waitlist_entry.user,
            notification_type=NotificationType.WAITLIST_SPOT_AVAILABLE,
            context=context,
        )

    logger.info(
        "waitlist_spot_available_notifications_sent",
        event_id=str(event.id),
        spots_available=spots_available,
        waitlist_count=waitlisted_users.count(),
    )


def _check_and_notify_waitlist(event_id: t.Any, old_count: int | None = None) -> None:
    """Check if event went from full to non-full and notify waitlist.

    Args:
        event_id: ID of the event to check
        old_count: Previous attendee count (before the change)
    """
    event = Event.objects.select_related("organization").get(pk=event_id)

    # Skip if event has no max attendees (unlimited capacity)
    if event.max_attendees == 0:
        return

    # Skip if waitlist is not open
    if not event.waitlist_open:
        return

    # Calculate current attendee count
    current_count = _calculate_attendee_count(event)

    # If we have old count, check if we went from full to non-full
    if old_count is not None:
        was_full = old_count >= event.max_attendees
        is_now_full = current_count >= event.max_attendees

        # Only notify if we went from full to non-full
        if was_full and not is_now_full:
            spots_available = event.max_attendees - current_count
            _notify_waitlist(event, spots_available)
    else:
        # If no old count (deletion case), just check if there are spots available now
        if current_count < event.max_attendees:
            spots_available = event.max_attendees - current_count
            # Only notify if there are actually people on the waitlist
            if EventWaitList.objects.filter(event=event).exists():
                _notify_waitlist(event, spots_available)


def _remove_user_from_waitlist(event_id: t.Any, user_id: t.Any) -> None:
    """Remove a user from event waitlist.

    Args:
        event_id: ID of the event
        user_id: ID of the user to remove
    """
    deleted_count, _ = EventWaitList.objects.filter(event_id=event_id, user_id=user_id).delete()
    if deleted_count > 0:
        logger.info("user_removed_from_waitlist", event_id=str(event_id), user_id=str(user_id))


# ===== Ticket Signal Handlers =====


@receiver(pre_save, sender=Ticket)
def capture_ticket_count_before_save(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Capture attendee count before ticket status change."""
    if instance.pk:
        try:
            old_instance = Ticket.objects.get(pk=instance.pk)
            # Only track if status is changing to/from ACTIVE
            if old_instance.status != instance.status:
                instance._old_attendee_count = _calculate_attendee_count(instance.event)  # type: ignore[attr-defined]
        except Ticket.DoesNotExist:
            logger.debug("ticket_not_found_for_attendee_count", pk=instance.pk)


@receiver(post_save, sender=Ticket)
def handle_ticket_waitlist_logic(sender: type[Ticket], instance: Ticket, created: bool, **kwargs: t.Any) -> None:
    """Handle waitlist logic when ticket is saved.

    Removal timing based on payment method:
    - Online payment: Remove when status becomes ACTIVE (payment completed)
    - Offline: Remove when created as PENDING (spot reserved)
    - Free/At-the-door: Remove when created as ACTIVE (immediate)

    Also notify waitlist if ticket cancellation freed up a spot.
    """

    def process_waitlist() -> None:
        # Remove user from waitlist based on ticket status and payment method
        should_remove = False

        if instance.status == Ticket.TicketStatus.ACTIVE:
            # Always remove for ACTIVE tickets (covers online payment completion, free, and at-the-door tickets)
            should_remove = True
        elif created and instance.status == Ticket.TicketStatus.PENDING:
            # Remove for newly created PENDING tickets with offline payment
            # (Online payment tickets start as PENDING but should only be removed when they become ACTIVE)
            # (AT_THE_DOOR tickets are now created as ACTIVE, so no special handling needed)
            from events.models import TicketTier

            if instance.tier.payment_method == TicketTier.PaymentMethod.OFFLINE:
                should_remove = True

        if should_remove:
            _remove_user_from_waitlist(instance.event_id, instance.user_id)

        # Check if we should notify waitlist (when count decreases and event was full)
        if hasattr(instance, "_old_attendee_count") and instance.status == Ticket.TicketStatus.CANCELLED:
            _check_and_notify_waitlist(instance.event_id, instance._old_attendee_count)

    transaction.on_commit(process_waitlist)


@receiver(post_delete, sender=Ticket)
def handle_ticket_deletion_waitlist(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Notify waitlist when ticket is deleted and spot becomes available."""

    def process_waitlist() -> None:
        # For deletions, we don't have the old count, so just check if spots are available
        _check_and_notify_waitlist(instance.event_id)

    transaction.on_commit(process_waitlist)


# ===== RSVP Signal Handlers =====


@receiver(pre_save, sender=EventRSVP)
def capture_rsvp_count_before_save(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Capture attendee count before RSVP status change."""
    if instance.pk:
        try:
            old_instance = EventRSVP.objects.get(pk=instance.pk)
            # Only track if status is changing
            if old_instance.status != instance.status:
                instance._old_rsvp_status = old_instance.status  # type: ignore[attr-defined]
                instance._old_attendee_count = _calculate_attendee_count(instance.event)  # type: ignore[attr-defined]
        except EventRSVP.DoesNotExist:
            logger.debug("rsvp_not_found_for_attendee_count", pk=instance.pk)


@receiver(post_save, sender=EventRSVP)
def handle_rsvp_waitlist_logic(sender: type[EventRSVP], instance: EventRSVP, created: bool, **kwargs: t.Any) -> None:
    """Handle waitlist logic when RSVP is saved.

    - Remove user from waitlist when RSVP is YES or NO (stay on MAYBE)
    - Notify waitlist if RSVP change freed up a spot (YES->NO or YES->MAYBE)
    """

    def process_waitlist() -> None:
        # Remove from waitlist if RSVP is YES or NO (MAYBE users stay on waitlist)
        if instance.status in (EventRSVP.RsvpStatus.YES, EventRSVP.RsvpStatus.NO):
            _remove_user_from_waitlist(instance.event_id, instance.user_id)

        # Check if we should notify waitlist (when YES changes to NO/MAYBE and event was full)
        if hasattr(instance, "_old_attendee_count") and hasattr(instance, "_old_rsvp_status"):
            old_status = instance._old_rsvp_status
            # Notify if changed from YES to NO/MAYBE (freed a spot)
            if old_status == EventRSVP.RsvpStatus.YES and instance.status != EventRSVP.RsvpStatus.YES:
                _check_and_notify_waitlist(instance.event_id, instance._old_attendee_count)

    transaction.on_commit(process_waitlist)


@receiver(post_delete, sender=EventRSVP)
def handle_rsvp_deletion_waitlist(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Notify waitlist when RSVP is deleted and spot becomes available."""

    def process_waitlist() -> None:
        # For deletions, we don't have the old count, so just check if spots are available
        _check_and_notify_waitlist(instance.event_id)

    transaction.on_commit(process_waitlist)
