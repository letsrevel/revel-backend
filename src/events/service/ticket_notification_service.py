"""Ticket notification service for handling ticket lifecycle notifications."""

import logging
from enum import Enum

from django.db import transaction

from events.models import Ticket, TicketTier
from events.service.notification_service import get_organization_staff_and_owners
from notifications.enums import NotificationType
from notifications.signals import notification_requested

logger = logging.getLogger(__name__)


class TicketNotificationAction(Enum):
    """Actions that can trigger ticket notifications."""

    FREE_TICKET_CREATED = "free_ticket_created"
    OFFLINE_PAYMENT_PENDING = "offline_payment_pending"
    AT_DOOR_PAYMENT_PENDING = "at_door_payment_pending"
    TICKET_ACTIVATED = "ticket_activated"


def handle_ticket_created(ticket: Ticket) -> None:
    """Handle notifications when a ticket is first created.

    Sends notifications to:
    - Ticket holder (always)
    - Organization staff/owners (if they have ticket notifications enabled)

    Args:
        ticket: The newly created ticket
    """
    tier = ticket.tier
    event = ticket.event

    # Determine attachment flags based on payment method
    if tier.payment_method == TicketTier.PaymentMethod.FREE:
        action = TicketNotificationAction.FREE_TICKET_CREATED.value
        include_pdf = True
        include_ics = True

    elif tier.payment_method == TicketTier.PaymentMethod.OFFLINE:
        action = TicketNotificationAction.OFFLINE_PAYMENT_PENDING.value
        include_pdf = False  # No PDF until payment is confirmed
        include_ics = True

    elif tier.payment_method == TicketTier.PaymentMethod.AT_THE_DOOR:
        action = TicketNotificationAction.AT_DOOR_PAYMENT_PENDING.value
        include_pdf = True  # They have reserved spot, so include PDF
        include_ics = True

    else:
        # Online payment - let the payment system handle notifications
        logger.debug(f"Skipping notification for online payment ticket {ticket.id}")
        return

    logger.info(f"Sending {action} notification for ticket {ticket.id}")

    # Schedule notifications to send after transaction commits
    def send_notifications() -> None:
        # Prepare context
        context = {
            "ticket_id": str(ticket.id),
            "ticket_reference": str(ticket.id),
            "event_id": str(event.id),
            "event_name": event.name,
            "event_start": event.start.isoformat() if event.start else "",
            "tier_name": tier.name,
            "action": action,
            "include_pdf": include_pdf,
            "include_ics": include_ics,
        }

        # Always notify ticket holder
        notification_requested.send(
            sender=handle_ticket_created,
            user=ticket.user,
            notification_type=NotificationType.TICKET_CREATED,
            context=context,
        )

        # Notify organization staff/owners if they have it enabled
        staff_and_owners = get_organization_staff_and_owners(event.organization_id)
        for staff_user in staff_and_owners:
            # Check if user has ticket notifications enabled
            try:
                prefs = staff_user.notification_preferences
                if prefs.is_notification_type_enabled(NotificationType.TICKET_CREATED.value):
                    notification_requested.send(
                        sender=handle_ticket_created,
                        user=staff_user,
                        notification_type=NotificationType.TICKET_CREATED,
                        context={
                            **context,
                            "ticket_holder_name": ticket.user.get_full_name() or ticket.user.username,
                            "ticket_holder_email": ticket.user.email,
                        },
                    )
            except Exception:
                # User may not have notification preferences yet, skip
                pass

    transaction.on_commit(send_notifications)


def handle_ticket_status_change(ticket: Ticket, old_status: str) -> None:
    """Handle notifications when a ticket status changes.

    Sends notification to ticket holder when ticket is activated.
    Staff/owners are not notified for status changes (only for creation).

    Args:
        ticket: The ticket with updated status
        old_status: The previous status of the ticket
    """
    # Only notify if status changed to ACTIVE from a non-ACTIVE status
    if ticket.status == Ticket.TicketStatus.ACTIVE and old_status != Ticket.TicketStatus.ACTIVE:
        logger.info(f"Ticket {ticket.id} activated (was {old_status}), sending notification")

        event = ticket.event
        tier = ticket.tier

        # Schedule notification to send after transaction commits
        def send_notification() -> None:
            notification_requested.send(
                sender=handle_ticket_status_change,
                user=ticket.user,
                notification_type=NotificationType.TICKET_UPDATED,
                context={
                    "ticket_id": str(ticket.id),
                    "ticket_reference": str(ticket.id),
                    "event_id": str(event.id),
                    "event_name": event.name,
                    "event_start": event.start.isoformat() if event.start else "",
                    "tier_name": tier.name,
                    "action": TicketNotificationAction.TICKET_ACTIVATED.value,
                    "include_pdf": True,
                    "include_ics": True,
                },
            )

        transaction.on_commit(send_notification)
    else:
        logger.debug(f"No notification needed for ticket {ticket.id} status change: {old_status} -> {ticket.status}")


def notify_ticket_creation(ticket_id: str) -> None:
    """Public interface for notifying about ticket creation.

    Args:
        ticket_id: The ID of the created ticket
    """
    try:
        ticket = Ticket.objects.select_related("tier").get(pk=ticket_id)
        handle_ticket_created(ticket)
    except Ticket.DoesNotExist:
        logger.error(f"Ticket with ID {ticket_id} not found for notification")


def notify_ticket_status_change(ticket_id: str, old_status: str) -> None:
    """Public interface for notifying about ticket status changes.

    Args:
        ticket_id: The ID of the ticket
        old_status: The previous status of the ticket
    """
    try:
        ticket = Ticket.objects.select_related("tier").get(pk=ticket_id)
        handle_ticket_status_change(ticket, old_status)
    except Ticket.DoesNotExist:
        logger.error(f"Ticket with ID {ticket_id} not found for status change notification")
