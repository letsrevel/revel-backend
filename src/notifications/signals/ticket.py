"""Signal handlers for ticket notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Event, Ticket, TicketTier
from notifications.enums import NotificationType
from notifications.service.eligibility import get_organization_staff_and_owners
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def _get_ticket_action_for_payment_method(payment_method: str) -> str | None:
    """Get action string for ticket creation based on payment method.

    Args:
        payment_method: Payment method from TicketTier.PaymentMethod

    Returns:
        Action string for notification, or None for online payments
    """
    action_map: dict[str, str] = {
        TicketTier.PaymentMethod.FREE: "free_ticket_created",
        TicketTier.PaymentMethod.OFFLINE: "offline_payment_pending",
        TicketTier.PaymentMethod.AT_THE_DOOR: "at_door_payment_pending",
    }
    return action_map.get(payment_method)


def _build_base_event_context(event: Event) -> dict[str, t.Any]:
    """Build common event context fields used across notifications.

    Args:
        event: Event to build context for

    Returns:
        Dictionary with event_id, event_name, event_location, event_url, event_start_formatted,
        organization_id, organization_name
    """
    from django.utils.dateformat import format as date_format

    event_location = event.full_address()
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    event_start_formatted = ""
    if event.start:
        event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T")

    return {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_location": event_location,
        "event_url": frontend_url,
        "event_start_formatted": event_start_formatted,
        "organization_id": str(event.organization_id),
        "organization_name": event.organization.name,
    }


def _build_ticket_created_context(ticket: Ticket) -> dict[str, t.Any]:
    """Build notification context for TICKET_CREATED."""
    event = ticket.event
    base_context = _build_base_event_context(event)

    # Get manual payment instructions if available
    manual_payment_instructions = None
    if ticket.tier.manual_payment_instructions:
        manual_payment_instructions = ticket.tier.manual_payment_instructions

    return {
        **base_context,
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_start": event.start.isoformat() if event.start else "",
        "tier_name": ticket.tier.name,
        "tier_price": str(ticket.tier.price),
        "ticket_status": ticket.status,
        "quantity": 1,  # Single ticket per notification
        "total_price": str(ticket.tier.price),
        "frontend_url": base_context["event_url"],  # Alias for backwards compatibility
        "payment_method": ticket.tier.payment_method,
        "manual_payment_instructions": manual_payment_instructions,
    }


def _build_ticket_updated_context(ticket: Ticket, old_status: str) -> dict[str, t.Any]:
    """Build notification context for TICKET_UPDATED."""
    event = ticket.event
    base_context = _build_base_event_context(event)

    # Determine action based on status change
    action = "updated"
    if old_status == Ticket.TicketStatus.PENDING and ticket.status == Ticket.TicketStatus.ACTIVE:
        action = "activated"
    elif ticket.status == Ticket.TicketStatus.CANCELLED:
        action = "cancelled"
    elif ticket.status == Ticket.TicketStatus.CHECKED_IN:
        action = "checked in"

    return {
        **base_context,
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "tier_name": ticket.tier.name,
        "ticket_status": ticket.status,
        "old_status": old_status,
        "new_status": ticket.status,
        "action": action,
    }


def _build_ticket_refunded_context(ticket: Ticket, refund_amount: str | None = None) -> dict[str, t.Any]:
    """Build notification context for TICKET_REFUNDED."""
    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(ticket.event.id),
        "event_name": ticket.event.name,
        "refund_amount": refund_amount or str(ticket.tier.price),
    }


def _build_ticket_checked_in_context(ticket: Ticket) -> dict[str, t.Any]:
    """Build notification context for TICKET_CHECKED_IN."""
    from django.utils.dateformat import format as date_format

    event = ticket.event
    base_context = _build_base_event_context(event)

    checked_in_at_formatted = ""
    if ticket.checked_in_at:
        checked_in_at_formatted = date_format(ticket.checked_in_at, "l, F j, Y \\a\\t g:i A T")

    return {
        **base_context,
        "ticket_id": str(ticket.id),
        "checked_in_at": checked_in_at_formatted,
    }


def _send_ticket_created_notifications(ticket: Ticket) -> None:
    """Send notifications for newly created ticket.

    Notifies both the ticket holder and organization staff/owners.
    Only sends notifications for offline/free tiers (online payment handled by payment service).

    Args:
        ticket: The newly created ticket
    """
    action = _get_ticket_action_for_payment_method(ticket.tier.payment_method)
    if not action:
        return  # Online payment - handled by payment service

    context = _build_ticket_created_context(ticket)

    # Notify ticket holder
    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_CREATED,
        context=context,
    )

    # Notify staff/owners with additional context (no attachments for staff)
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
        "include_pdf": False,
        "include_ics": False,
    }
    staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
    for staff_user in staff_and_owners:
        if staff_user.notification_preferences.is_notification_type_enabled(NotificationType.TICKET_CREATED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_CREATED,
                context=staff_context,
            )


def _send_ticket_activated_notification(ticket: Ticket, old_status: str) -> None:
    """Send notification when ticket is activated.

    Notifies the ticket holder when their ticket status changes to ACTIVE.
    For online payments (PENDING→ACTIVE), skip this notification as the user
    already receives PAYMENT_CONFIRMATION from the payment service.

    Args:
        ticket: The ticket being activated
        old_status: The previous ticket status
    """
    # Skip notification for online payment activations (user gets PAYMENT_CONFIRMATION instead)
    if old_status == Ticket.TicketStatus.PENDING and ticket.tier.payment_method == TicketTier.PaymentMethod.ONLINE:
        return

    context = _build_ticket_updated_context(ticket, old_status)

    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_UPDATED,
        context=context,
    )


def _send_ticket_cancelled_notifications(ticket: Ticket, old_status: str) -> None:
    """Send notifications when ticket is cancelled.

    Notifies both the ticket holder and organization staff/owners about the cancellation.

    Args:
        ticket: The ticket being cancelled
        old_status: The previous ticket status
    """
    context = _build_ticket_updated_context(ticket, old_status)

    # Notify ticket holder
    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_CANCELLED,
        context=context,
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
    }
    staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
    for staff_user in staff_and_owners:
        if staff_user.notification_preferences.is_notification_type_enabled(NotificationType.TICKET_CANCELLED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_CANCELLED,
                context=staff_context,
            )


def _send_ticket_refunded_notifications(ticket: Ticket) -> None:
    """Send notifications when ticket is refunded.

    Notifies both the ticket holder and organization staff/owners about the refund.
    Includes refund amount from ticket._refund_amount if available.

    Args:
        ticket: The ticket being refunded
    """
    # Add refund amount if available
    refund_amount_value = getattr(ticket, "_refund_amount", None)
    context = _build_ticket_refunded_context(ticket, refund_amount_value)

    # Notify ticket holder
    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_REFUNDED,
        context=context,
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
    }
    staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
    for staff_user in staff_and_owners:
        if staff_user.notification_preferences.is_notification_type_enabled(NotificationType.TICKET_REFUNDED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_REFUNDED,
                context=staff_context,
            )


def _send_ticket_checked_in_notification(ticket: Ticket) -> None:
    """Send notification when ticket is checked in.

    Args:
        ticket: The ticket being checked in
    """
    context = _build_ticket_checked_in_context(ticket)

    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_CHECKED_IN,
        context=context,
    )


def _handle_ticket_status_change(ticket: Ticket, old_status: str | None) -> None:
    """Handle notifications for ticket status changes."""
    if not old_status or old_status == ticket.status:
        return

    if ticket.status == Ticket.TicketStatus.ACTIVE:
        _send_ticket_activated_notification(ticket, old_status)

        # If payment was just completed (PENDING→ACTIVE), notify staff/owners
        if old_status == Ticket.TicketStatus.PENDING:
            context = _build_ticket_created_context(ticket)
            staff_context = {
                **context,
                "ticket_holder_name": ticket.user.get_display_name(),
                "ticket_holder_email": ticket.user.email,
                "include_pdf": False,
                "include_ics": False,
            }
            staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
            for staff_user in list(staff_and_owners):
                prefs = getattr(staff_user, "notification_preferences", None)
                if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_CREATED):
                    notification_requested.send(
                        sender=Ticket,
                        user=staff_user,
                        notification_type=NotificationType.TICKET_CREATED,
                        context=staff_context,
                    )
    elif ticket.status == Ticket.TicketStatus.CHECKED_IN:
        _send_ticket_checked_in_notification(ticket)
    elif ticket.status == Ticket.TicketStatus.CANCELLED:
        # Refund notifications are handled by Payment signals in notifications/signals/payment.py
        # Only send cancellation notifications for non-refund cancellations
        _send_ticket_cancelled_notifications(ticket, old_status)


@receiver(pre_save, sender=Ticket)
def capture_ticket_old_status(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save.

    NOTE: This pattern uses pre_save to fetch the old instance, which theoretically
    has a race condition if another transaction modifies the ticket between the read
    and the save. However, we don't expect concurrent modifications to ticket status
    in our current use cases, so this simpler approach is preferred over adding
    django-model-utils dependency for FieldTracker.
    """
    if instance.pk:
        try:
            old_instance = Ticket.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except Ticket.DoesNotExist:
            pass


@receiver(post_save, sender=Ticket)
def handle_ticket_notifications(sender: type[Ticket], instance: Ticket, created: bool, **kwargs: t.Any) -> None:
    """Send notifications for ticket lifecycle events.

    Note: This is one of multiple post_save handlers for Ticket model:
    - events.signals.handle_ticket_visibility_and_potluck: Handles visibility flags + potluck unclaiming
    - notifications.signals.ticket.handle_ticket_notifications: Sends notifications (this handler)
    - notifications.signals.waitlist.handle_ticket_waitlist_logic: Manages waitlist removal

    Visibility flags are handled by events.signals to avoid duplication.
    """

    def send_notifications() -> None:
        if created:
            _send_ticket_created_notifications(instance)
        else:
            # Check if old status was captured in pre_save
            if hasattr(instance, "_old_status"):
                _handle_ticket_status_change(instance, instance._old_status)

    transaction.on_commit(send_notifications)


# Note: TICKET_REFUNDED notifications are handled by Payment model signals
# in the stripe service when a refund is processed, not by Ticket model
