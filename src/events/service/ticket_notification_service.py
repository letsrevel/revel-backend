"""Ticket notification service for handling ticket lifecycle notifications."""

import logging
from enum import Enum

from events.models import Ticket, TicketTier
from events.tasks import notify_ticket_update

logger = logging.getLogger(__name__)


class TicketNotificationAction(Enum):
    """Actions that can trigger ticket notifications."""

    FREE_TICKET_CREATED = "free_ticket_created"
    OFFLINE_PAYMENT_PENDING = "offline_payment_pending"
    AT_DOOR_PAYMENT_PENDING = "at_door_payment_pending"
    TICKET_ACTIVATED = "ticket_activated"


def handle_ticket_created(ticket: Ticket) -> None:
    """Handle notifications when a ticket is first created.

    Args:
        ticket: The newly created ticket
    """
    tier = ticket.tier

    # Determine the appropriate notification action
    if tier.payment_method == TicketTier.PaymentMethod.FREE:
        # Free ticket - notify immediately with PDF and ICS
        action = TicketNotificationAction.FREE_TICKET_CREATED
        include_pdf = True
        include_ics = True

    elif tier.payment_method == TicketTier.PaymentMethod.OFFLINE:
        # Pay offline - notify with payment instructions
        action = TicketNotificationAction.OFFLINE_PAYMENT_PENDING
        include_pdf = False  # No PDF until payment is confirmed
        include_ics = True

    elif tier.payment_method == TicketTier.PaymentMethod.AT_THE_DOOR:
        # Pay at door - notify with instructions and PDF (they have reserved spot)
        action = TicketNotificationAction.AT_DOOR_PAYMENT_PENDING
        include_pdf = True  # They have reserved spot, so include PDF
        include_ics = True

    else:
        # Online payment - let the payment system handle notifications
        logger.debug(f"Skipping notification for online payment ticket {ticket.id}")
        return

    logger.info(f"Sending {action.value} notification for ticket {ticket.id}")
    notify_ticket_update.delay(
        ticket_id=str(ticket.id), action=action.value, include_pdf=include_pdf, include_ics=include_ics
    )


def handle_ticket_status_change(ticket: Ticket, old_status: str) -> None:
    """Handle notifications when a ticket status changes.

    Args:
        ticket: The ticket with updated status
        old_status: The previous status of the ticket
    """
    # Only notify if status changed to ACTIVE from a non-ACTIVE status
    if ticket.status == Ticket.TicketStatus.ACTIVE and old_status != Ticket.TicketStatus.ACTIVE:
        logger.info(f"Ticket {ticket.id} activated (was {old_status}), sending notification")
        notify_ticket_update.delay(
            ticket_id=str(ticket.id),
            action=TicketNotificationAction.TICKET_ACTIVATED.value,
            include_pdf=True,
            include_ics=True,
        )
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
