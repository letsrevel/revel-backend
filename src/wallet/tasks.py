"""Celery tasks for wallet pass operations.

These tasks handle async operations like sending push notifications
to update wallet passes when event details change.
"""

from uuid import UUID

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    name="wallet.send_update_notifications_for_event",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def send_wallet_update_notifications_for_event(self: object, event_id: str) -> dict[str, int]:
    """Send wallet pass update notifications for all tickets to an event.

    This task is triggered when event details change (time, location, etc.)
    and notifies all devices that have passes for tickets to this event.

    Args:
        self: Celery task instance (bound task).
        event_id: The UUID of the event that was updated.

    Returns:
        Dictionary with 'notifications_sent' count.
    """
    from wallet.service import get_wallet_service

    logger.info("sending_wallet_update_notifications", event_id=event_id)

    service = get_wallet_service()

    try:
        event_uuid = UUID(event_id)
        count = service.send_update_notifications_for_event(event_uuid)

        logger.info(
            "wallet_update_notifications_complete",
            event_id=event_id,
            notifications_sent=count,
        )

        return {"notifications_sent": count}

    except Exception as e:
        logger.error(
            "wallet_update_notifications_failed",
            event_id=event_id,
            error=str(e),
        )
        raise


@shared_task(
    name="wallet.send_update_notification_for_ticket",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def send_wallet_update_notification_for_ticket(self: object, ticket_id: str) -> dict[str, int]:
    """Send wallet pass update notification for a specific ticket.

    Use this when a ticket's status changes (e.g., cancelled, checked in).

    Args:
        self: Celery task instance (bound task).
        ticket_id: The UUID of the ticket that was updated.

    Returns:
        Dictionary with 'notifications_sent' count.
    """
    from wallet.service import get_wallet_service

    logger.info("sending_wallet_update_for_ticket", ticket_id=ticket_id)

    service = get_wallet_service()

    try:
        ticket_uuid = UUID(ticket_id)
        count = service.send_update_notification_for_ticket(ticket_uuid)

        return {"notifications_sent": count}

    except Exception as e:
        logger.error(
            "wallet_ticket_notification_failed",
            ticket_id=ticket_id,
            error=str(e),
        )
        raise


@shared_task(name="wallet.cleanup_expired_registrations")
def cleanup_expired_registrations() -> dict[str, int]:
    """Clean up registrations for expired/cancelled tickets.

    This periodic task removes registrations for tickets that are no longer
    valid (cancelled, event past, etc.) to keep the database clean.

    Returns:
        Dictionary with 'deleted' count.
    """
    from datetime import timedelta

    from django.utils import timezone

    from events.models import Ticket
    from wallet.models import WalletPassRegistration

    logger.info("cleaning_up_wallet_registrations")

    # Find registrations for cancelled tickets
    cancelled_regs = WalletPassRegistration.objects.filter(ticket__status=Ticket.TicketStatus.CANCELLED)

    # Find registrations for past events (more than 7 days ago)
    week_ago = timezone.now() - timedelta(days=7)
    past_event_regs = WalletPassRegistration.objects.filter(ticket__event__end__lt=week_ago)

    # Combine and delete
    to_delete = cancelled_regs | past_event_regs
    count = to_delete.count()

    if count > 0:
        to_delete.delete()
        logger.info("wallet_registrations_cleaned", deleted=count)

    return {"deleted": count}
