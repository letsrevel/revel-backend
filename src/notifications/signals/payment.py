"""Signal handlers for payment and refund notifications."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Payment
from notifications.enums import NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


@receiver(pre_save, sender=Payment)
def capture_payment_old_status(sender: type[Payment], instance: Payment, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection."""
    if instance.pk:
        try:
            old_instance = Payment.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except Payment.DoesNotExist:
            pass


@receiver(post_save, sender=Payment)
def handle_payment_status_change(sender: type[Payment], instance: Payment, created: bool, **kwargs: t.Any) -> None:
    """Handle payment status changes and send appropriate notifications.

    - SUCCEEDED: Send PAYMENT_CONFIRMATION to ticket holder
    - REFUNDED: Send TICKET_REFUNDED to ticket holder and staff/owners
    """
    # Skip if no status change
    if not hasattr(instance, "_old_status"):
        return

    old_status = instance._old_status

    # Skip if status didn't actually change
    if old_status == instance.status:
        return

    def send_notifications() -> None:
        """Send notifications after transaction commits."""
        if instance.status == Payment.PaymentStatus.SUCCEEDED:
            _send_payment_confirmation(instance)
        elif instance.status == Payment.PaymentStatus.REFUNDED:
            _send_refund_notifications(instance)

    transaction.on_commit(send_notifications)


def _send_payment_confirmation(payment: Payment) -> None:
    """Send payment confirmation notification to ticket holder.

    Args:
        payment: The successful payment instance
    """
    from django.utils.dateformat import format as date_format

    ticket = payment.ticket
    event = ticket.event

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    # Format event details
    event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
    event_location = event.full_address()

    # Format payment date
    payment_date = date_format(payment.created_at, "l, F j, Y \\a\\t g:i A T")

    notification_requested.send(
        sender=_send_payment_confirmation,
        user=payment.user,
        notification_type=NotificationType.PAYMENT_CONFIRMATION,
        context={
            "ticket_id": str(ticket.id),
            "ticket_reference": str(ticket.id),
            "event_id": str(event.id),
            "event_name": event.name,
            "event_start": event.start.isoformat() if event.start else "",
            "event_start_formatted": event_start_formatted,
            "event_location": event_location,
            "event_url": frontend_url,
            "tier_name": ticket.tier.name,
            "payment_amount": str(payment.amount),
            "payment_currency": payment.currency,
            "payment_id": str(payment.id),
            "payment_date": payment_date,
            "payment_method": "card",  # Stripe payments are card-based
        },
    )

    logger.info(
        "payment_confirmation_sent",
        payment_id=str(payment.id),
        ticket_id=str(ticket.id),
        user_id=str(payment.user_id),
    )


def _send_refund_notifications(payment: Payment) -> None:
    """Send refund notifications to ticket holder and staff/owners.

    Args:
        payment: The refunded payment instance
    """
    ticket = payment.ticket
    event = ticket.event

    refund_amount = f"{payment.amount} {payment.currency}"

    context = {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "refund_amount": refund_amount,
    }

    # Notify ticket holder
    notification_requested.send(
        sender=_send_refund_notifications,
        user=payment.user,
        notification_type=NotificationType.TICKET_REFUNDED,
        context=context,
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": payment.user.get_display_name(),
        "ticket_holder_email": payment.user.email,
    }

    staff_and_owners = get_staff_for_notification(event.organization_id, NotificationType.TICKET_REFUNDED)
    for staff_user in staff_and_owners:
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_REFUNDED):
            notification_requested.send(
                sender=_send_refund_notifications,
                user=staff_user,
                notification_type=NotificationType.TICKET_REFUNDED,
                context=staff_context,
            )

    logger.info(
        "refund_notifications_sent",
        payment_id=str(payment.id),
        ticket_id=str(ticket.id),
        user_id=str(payment.user_id),
    )
