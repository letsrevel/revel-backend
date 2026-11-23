"""Signal handlers for payment and refund notifications."""

import typing as t
from decimal import Decimal
from urllib.parse import quote

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from events.models import Dispute, Event, Organization, Payment, Ticket
from events.utils import format_organization_datetime
from events.utils.currency import format_major_amount
from events.utils.erased_holder import holder_email
from events.utils.frontend_urls import frontend_base_url_for
from notifications.context_schemas import (
    EventRefundSummaryContext,
    RefundFailedContext,
    RefundUnmatchedCandidate,
    StaffPaymentDisputedContext,
    TicketRefundFailedContext,
)
from notifications.enums import NotificationType
from notifications.service.notification_helpers import (
    format_event_datetime,
    get_event_location_for_user,
    notify_org_staff,
)
from notifications.signals import notification_requested
from notifications.signals.ticket import staff_holder_name

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
    - REFUNDED: Send TICKET_REFUNDED to ticket holder and STAFF_TICKET_REFUNDED to staff/owners
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
    if ticket.status == Ticket.TicketStatus.CANCELLED:
        return  # a late payment on a cancelled ticket is refunded, not confirmed (#1459)

    event = ticket.event

    # Attendee-facing: the link follows the organization's custom domain (no token on it).
    frontend_url = f"{frontend_base_url_for(event.organization)}/events/{event.id}"

    # Format event details in event's timezone
    event_start_formatted = format_event_datetime(event.start, event)
    event_location, _ = get_event_location_for_user(event, payment.user)

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
            "organization_id": str(event.organization_id),
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
        # organization_id lets enrichment point event_url at the org's custom domain (#1153).
        context={**context, "organization_id": str(event.organization_id)},
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": staff_holder_name(ticket),
        "ticket_holder_email": holder_email(ticket),
    }

    notify_org_staff(
        organization_id=event.organization_id,
        notification_type=NotificationType.STAFF_TICKET_REFUNDED,
        context=staff_context,
        sender=_send_refund_notifications,
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
    # Deep-link to the event's ticket admin filtered to the buyer (#744). Every Payment on
    # one intent belongs to one buyer, so their email narrows the list to exactly the
    # candidates below. `?search=` is already read by that page and `list_tickets`' search
    # covers `user__email` — no new surface invented for a notification.
    resolve_url = (
        f"{SiteSettings.get_solo().frontend_base_url}"
        f"/org/{organization.slug}/admin/events/{tickets[0].event_id}/tickets"
        f"?search={quote(tickets[0].user.email)}"
    )
    candidate_contexts: list[RefundUnmatchedCandidate] = [
        {
            "ticket_id": str(ticket.id),
            "event_name": ticket.event.name,
            "seat_label": ticket.seat.label if ticket.seat else "",
            "amount": str(amount_by_ticket[ticket.id]),
            "holder_email": holder_email(ticket),
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
        "resolve_url": resolve_url,
    }

    notified = notify_org_staff(
        organization_id=organization.id,
        notification_type=NotificationType.REFUND_UNMATCHED,
        context=context,
        sender=send_refund_unmatched,
    )

    logger.info(
        "refund_unmatched_notifications_sent",
        payment_intent_id=payment_intent_id,
        refund_id=refund_id,
        reason=reason,
        candidate_count=len(candidate_contexts),
        recipient_count=notified,
    )


def send_refund_failed(
    *,
    organization: Organization,
    refund_id: str,
    payment_intent_id: str,
    refund_amount: Decimal,
    currency: str,
    failure_reason: str,
    holder_name: str,
    holder_email: str,
    ticket: Ticket | None = None,
    plan_name: str = "",
    credit_note_numbers: t.Sequence[str] = (),
    subscription_cancelled_by_refund: bool = False,
) -> int:
    """Notify org staff and owners that a refund failed at Stripe after Revel recorded it (#1507).

    Called explicitly from the refund webhook handlers, inside their atomic block (same
    rationale as :func:`send_refund_unmatched`). The recipients are the owners and the staff with
    ``manage_tickets``, for ticket and membership refunds alike.

    Args:
        organization: The organization that owns the payment.
        refund_id: The Stripe refund id.
        payment_intent_id: The Stripe payment intent id.
        refund_amount: The refund amount in major currency units.
        currency: ISO currency code of the refund.
        failure_reason: The Stripe failure reason, or an empty string.
        holder_name: The name staff see for the buyer.
        holder_email: The buyer email.
        ticket: The ticket of a ticket refund. None for a membership refund.
        plan_name: The plan name of a membership refund.
        credit_note_numbers: Credit notes Revel issued for the refund. Revel does not reverse them.
        subscription_cancelled_by_refund: Whether a refund had cancelled the membership subscription.

    Returns:
        The number of staff members notified.
    """
    context: RefundFailedContext = {
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "kind": "ticket" if ticket is not None else "membership",
        "refund_id": refund_id,
        "payment_intent_id": payment_intent_id,
        "refund_amount": f"{refund_amount:.2f}",
        "currency": currency,
        "failure_reason": failure_reason,
        "holder_name": holder_name,
        "holder_email": holder_email,
    }
    if ticket is not None:
        context["event_id"] = str(ticket.event_id)
        context["event_name"] = ticket.event.name
        # Event ticket admin, filtered to the buyer (the same link as the unmatched-refund notification).
        context["resolve_url"] = (
            f"{SiteSettings.get_solo().frontend_base_url}"
            f"/org/{organization.slug}/admin/events/{ticket.event_id}/tickets"
            f"?search={quote(holder_email)}"
        )
    if plan_name:
        context["plan_name"] = plan_name
        context["subscription_cancelled_by_refund"] = subscription_cancelled_by_refund
    if credit_note_numbers:
        context["credit_note_numbers"] = list(credit_note_numbers)

    notified = notify_org_staff(
        organization_id=organization.id,
        notification_type=NotificationType.REFUND_FAILED,
        context=context,
        sender=send_refund_failed,
    )
    logger.info(
        "refund_failed_notifications_sent",
        refund_id=refund_id,
        kind=context["kind"],
        recipient_count=notified,
    )
    return notified


def send_ticket_refund_failed(*, payment: Payment, refund_amount: Decimal) -> None:
    """Tell the buyer that a refund failed, after an earlier "ticket refunded" email (#1507).

    The caller sends it only when that earlier email went out. A series-pass ticket never got it,
    so it gets no failure email either.

    Args:
        payment: The Payment whose refund failed.
        refund_amount: The failed refund amount in major currency units.
    """
    ticket = payment.ticket
    if ticket.held_pass_id is not None:
        return
    event = ticket.event
    context: TicketRefundFailedContext = {
        "ticket_id": str(ticket.id),
        "event_id": str(event.id),
        "event_name": event.name,
        # organization_id lets enrichment point event_url at the org's custom domain (#1153).
        "organization_id": str(event.organization_id),
        "refund_amount": f"{refund_amount:.2f} {payment.currency}",
    }
    notification_requested.send(
        sender=send_ticket_refund_failed,
        user=payment.user,
        notification_type=NotificationType.TICKET_REFUND_FAILED,
        context=context,
    )
    logger.info("ticket_refund_failed_notification_sent", payment_id=str(payment.id), ticket_id=str(ticket.id))


def send_payment_disputed(*, dispute: Dispute, outcome: str) -> None:
    """Notify org staff that a card dispute was opened or closed.

    Called explicitly from the ``charge.dispute.*`` webhook handler, inside its
    atomic block (same rationale as :func:`send_refund_unmatched`). A dispute
    with no resolved organization has nobody to notify and is skipped.

    Args:
        dispute: The upserted Dispute row.
        outcome: ``opened`` (evidence deadline pending) or ``closed`` (final status).
    """
    organization = dispute.organization
    if organization is None:
        return

    first_payment = dispute.linked_payments().select_related("ticket__event").first()
    context: StaffPaymentDisputedContext = {
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "dispute_id": dispute.stripe_dispute_id,
        "outcome": outcome,
        "status": dispute.status,
        "amount": format_major_amount(dispute.amount, dispute.currency),
        "currency": dispute.currency,
        "reason": dispute.reason,
        "evidence_due_by": format_organization_datetime(dispute.evidence_due_by, organization),
        "dashboard_url": f"https://dashboard.stripe.com/{Payment.stripe_mode()}/disputes/{dispute.stripe_dispute_id}",
    }
    if first_payment is not None:
        context["event_id"] = str(first_payment.ticket.event_id)
        context["event_name"] = first_payment.ticket.event.name

    notified = notify_org_staff(
        organization_id=organization.id,
        notification_type=NotificationType.STAFF_PAYMENT_DISPUTED,
        context=context,
        sender=send_payment_disputed,
    )
    logger.info(
        "payment_dispute_notifications_sent",
        dispute_id=dispute.stripe_dispute_id,
        outcome=outcome,
        recipient_count=notified,
    )


def send_event_refund_summary(
    *,
    event: Event,
    cancelled: int,
    refunded: int,
    failed: int,
    still_active: int,
) -> None:
    """Notify org staff that a bulk event-cancellation refund sweep has finished.

    Called from ``events.send_event_refund_summary`` (the Celery task in
    ``events/tasks/refunds.py``) once every ticket on the event is CANCELLED, or
    once that task's retry budget (30 attempts, 120s apart) is exhausted — in
    which case ``still_active`` is non-zero and the counts below are a
    best-effort snapshot rather than a final tally. A plain function like
    ``send_refund_unmatched``: the task runs outside any request transaction, so
    there is nothing to defer the ``notification_requested.send`` behind.

    Args:
        event: The cancelled event the sweep ran against.
        cancelled: Tickets whose ``cancellation_source`` is EVENT_CANCELLATION.
        refunded: Refund rows (source=EVENT_CANCELLATION) that are PENDING or SUCCEEDED.
        failed: Payments whose EVENT_CANCELLATION refund rows are all FAILED.
        still_active: Non-CANCELLED tickets remaining on the event (0 unless the
            sweep gave up waiting).
    """
    context: EventRefundSummaryContext = {
        "organization_id": str(event.organization_id),
        "organization_name": event.organization.name,
        "event_id": str(event.id),
        "event_name": event.name,
        "cancelled": cancelled,
        "refunded": refunded,
        "failed": failed,
        "still_active": still_active,
    }

    notified = notify_org_staff(
        organization_id=event.organization_id,
        notification_type=NotificationType.EVENT_REFUND_SUMMARY,
        context=context,
        sender=send_event_refund_summary,
    )

    logger.info(
        "event_refund_summary_notifications_sent",
        event_id=str(event.id),
        cancelled=cancelled,
        refunded=refunded,
        failed=failed,
        still_active=still_active,
        recipient_count=notified,
    )
