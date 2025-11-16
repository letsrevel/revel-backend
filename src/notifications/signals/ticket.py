"""Signal handlers for ticket notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Ticket, TicketTier
from events.tasks import build_attendee_visibility_flags
from notifications.enums import NotificationType
from notifications.service.eligibility import get_organization_staff_and_owners
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def _get_ticket_action_for_payment_method(payment_method: str) -> str | None:
    """Get action string for ticket creation based on payment method."""
    action_map = {
        TicketTier.PaymentMethod.FREE: "free_ticket_created",
        TicketTier.PaymentMethod.OFFLINE: "offline_payment_pending",
        TicketTier.PaymentMethod.AT_THE_DOOR: "at_door_payment_pending",
    }
    return t.cast(str | None, action_map.get(payment_method))  # type: ignore[call-overload]


def _build_ticket_created_context(ticket: Ticket) -> dict[str, t.Any]:
    """Build notification context for TICKET_CREATED."""
    event = ticket.event
    event_location = event.address or (event.city.name if event.city else "")

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start": event.start.isoformat() if event.start else "",
        "event_location": event_location,
        "organization_id": str(event.organization_id),
        "organization_name": event.organization.name,
        "tier_name": ticket.tier.name,
        "tier_price": str(ticket.tier.price),
        "quantity": 1,  # Single ticket per notification
        "total_price": str(ticket.tier.price),
        "frontend_url": frontend_url,
    }


def _build_ticket_updated_context(ticket: Ticket, old_status: str) -> dict[str, t.Any]:
    """Build notification context for TICKET_UPDATED."""
    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(ticket.event.id),
        "event_name": ticket.event.name,
        "old_status": old_status,
        "new_status": ticket.status,
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


def _send_ticket_created_notifications(ticket: Ticket) -> None:
    """Send notifications for newly created ticket."""
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

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
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


def _send_ticket_activated_notification(ticket: Ticket, old_status: str) -> None:
    """Send notification when ticket is activated."""
    context = _build_ticket_updated_context(ticket, old_status)

    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_UPDATED,
        context=context,
    )


def _send_ticket_cancelled_notifications(ticket: Ticket, old_status: str) -> None:
    """Send notifications when ticket is cancelled."""
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
    for staff_user in list(staff_and_owners):
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_CANCELLED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_CANCELLED,
                context=staff_context,
            )


def _send_ticket_refunded_notifications(ticket: Ticket) -> None:
    """Send notifications when ticket is refunded."""
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
    for staff_user in list(staff_and_owners):
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_REFUNDED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_REFUNDED,
                context=staff_context,
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
    elif ticket.status == Ticket.TicketStatus.CANCELLED:
        # Refund notifications are handled by Payment signals in notifications/signals/payment.py
        # Only send cancellation notifications for non-refund cancellations
        _send_ticket_cancelled_notifications(ticket, old_status)


@receiver(pre_save, sender=Ticket)
def capture_ticket_old_status(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save."""
    if instance.pk:
        try:
            old_instance = Ticket.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except Ticket.DoesNotExist:
            pass


@receiver(post_save, sender=Ticket)
def handle_ticket_save_and_notifications(
    sender: type[Ticket], instance: Ticket, created: bool, **kwargs: t.Any
) -> None:
    """Send notifications for ticket lifecycle events."""
    build_attendee_visibility_flags.delay(str(instance.event_id))

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
