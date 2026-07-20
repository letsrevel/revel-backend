"""Signal handlers for payment and refund notifications."""

import typing as t
from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Payment, Ticket
from notifications.context_schemas import RefundUnmatchedCandidate
from notifications.enums import NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.service.notification_helpers import format_event_datetime
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
            logger.debug("payment_not_found_for_old_status", pk=instance.pk)


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
    ticket = payment.ticket
    if ticket.held_pass_id is not None:
        return  # Series pass tickets get one pass-level notification (see notifications/signals/series_pass.py)

    event = ticket.event

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    # Format event details in event's timezone
    event_start_formatted = format_event_datetime(event.start, event)
    event_location = event.full_address()

    # Format payment date in event's timezone
    payment_date = format_event_datetime(payment.created_at, event)

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
    if ticket.held_pass_id is not None:
        return  # Series pass tickets get one pass-level notification (see notifications/signals/series_pass.py)

    event = ticket.event

    actual_amount = payment.refund_amount if payment.refund_amount is not None else payment.amount
    refund_amount = f"{actual_amount} {payment.currency}"

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


def send_refund_unmatched(
    *,
    payment_intent_id: str,
    refund_id: str,
    refund_amount: Decimal,
    currency: str,
    reason: str,
    candidates: list[Payment],
) -> None:
    """Notify org staff that an inbound refund could not be matched to a ticket.

    Unlike the receivers above this is a plain function, called explicitly from
    the ``charge.refunded`` webhook handler when the matcher declines to guess
    (see ``StripeEventHandler._match_refund_to_payments``): nothing was
    cancelled and no seat was freed, so the only trace would otherwise be a log
    line. Refunds carrying ``metadata.ticket_id`` — every refund issued through
    Revel — match exactly and never reach here.

    Call from *inside* the webhook's atomic block: the Notification rows are
    written in the same transaction (so a rolled-back webhook leaves no false
    alarm) while the dispatcher defers the Celery ``.delay()`` to ``on_commit``.

    Args:
        payment_intent_id: The Stripe payment intent the refund arrived on.
        refund_id: The Stripe refund object id.
        refund_amount: The refunded amount in major currency units.
        currency: ISO currency code of the refund.
        reason: ``non_uniform`` or ``ambiguous`` — why the match was declined.
        candidates: The unrefunded Payments the refund could have applied to.
    """
    amount_by_ticket = {p.ticket_id: p.amount for p in candidates}
    tickets = list(
        Ticket.objects.filter(pk__in=amount_by_ticket)
        .select_related("event__organization", "seat", "user")
        .order_by("event__name", "seat__label")
    )
    if not tickets:  # pragma: no cover - candidates always carry a ticket (OneToOne, cascade)
        return

    organization = tickets[0].event.organization
    candidate_contexts: list[RefundUnmatchedCandidate] = [
        {
            "ticket_id": str(ticket.id),
            "event_name": ticket.event.name,
            "seat_label": ticket.seat.label if ticket.seat else "",
            "amount": str(amount_by_ticket[ticket.id]),
            "holder_email": ticket.user.email,
        }
        for ticket in tickets
    ]
    context: dict[str, t.Any] = {
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "payment_intent_id": payment_intent_id,
        "refund_id": refund_id,
        # Two decimals like every Payment.amount rendered above — from_stripe_amount
        # returns an unpadded Decimal (3000 cents -> Decimal("30")).
        "refund_amount": f"{refund_amount:.2f}",
        "currency": currency,
        "reason": reason,
        "candidates": candidate_contexts,
    }

    recipients = get_staff_for_notification(organization.id, NotificationType.REFUND_UNMATCHED)
    notified = 0
    for staff_user in recipients:
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.REFUND_UNMATCHED):
            notification_requested.send(
                sender=send_refund_unmatched,
                user=staff_user,
                notification_type=NotificationType.REFUND_UNMATCHED,
                context=context,
            )
            notified += 1

    logger.info(
        "refund_unmatched_notifications_sent",
        payment_intent_id=payment_intent_id,
        refund_id=refund_id,
        reason=reason,
        candidate_count=len(candidate_contexts),
        recipient_count=notified,
    )
