"""Bulk cancel-and-refund sweep for cancelled events."""

import structlog
from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from events.exceptions import NothingToRefundError, RefundInsufficientBalanceError, StripeRefundFailed
from events.models import Payment, Refund, Ticket, TicketTier
from events.models.ticket import CancellationSource
from events.service import refund_service

logger = structlog.get_logger(__name__)


@shared_task(name="events.refund_cancelled_event_tickets")
def refund_cancelled_event_tickets(event_id: str, initiated_by_id: str | None = None) -> dict[str, int]:
    """Cancel every non-cancelled ticket of a cancelled event and refund online payments.

    One DB transaction per ticket — no lock is ever held across another row's
    Stripe call. Re-entrant: already-cancelled tickets are skipped and
    ``remaining_refundable`` short-circuits already-refunded payments, so a
    crashed run can simply be re-dispatched.

    Per-ticket Stripe failures are recorded as FAILED Refund rows (retryable via
    the single-refund endpoint) and the sweep continues.
    """
    from accounts.models import RevelUser

    initiator = RevelUser.objects.filter(pk=initiated_by_id).first() if initiated_by_id else None
    ticket_ids = list(
        Ticket.objects.filter(event_id=event_id)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("id", flat=True)
    )
    cancelled = refunded = failed = 0

    for ticket_id in ticket_ids:
        payment_pk = None
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().select_related("tier", "event").filter(pk=ticket_id).first()
            if ticket is None or ticket.status == Ticket.TicketStatus.CANCELLED:
                continue
            payment = Payment.objects.select_for_update().filter(ticket=ticket).first()
            if payment is not None and payment.status == Payment.PaymentStatus.PENDING:
                payment.status = Payment.PaymentStatus.FAILED
                payment.save(update_fields=["status"])
            elif payment is not None:
                payment_pk = payment.pk

            TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.cancelled_at = timezone.now()
            ticket.cancelled_by = initiator
            ticket.cancellation_source = CancellationSource.EVENT_CANCELLATION
            ticket.cancellation_reason = ticket.event.cancellation_reason or ""
            ticket.save(
                update_fields=[
                    "status",
                    "cancelled_at",
                    "cancelled_by",
                    "cancellation_source",
                    "cancellation_reason",
                ]
            )
            cancelled += 1
            # Deliberately NO waitlist enqueue: the event is cancelled; freed seats are not for sale.

        if payment_pk is None:
            continue
        payment = Payment.objects.get(pk=payment_pk)
        try:
            with transaction.atomic():
                refund_service.issue_refund(
                    payment,
                    amount=None,
                    initiated_by=initiator,
                    reason="event_cancelled",
                    source=Refund.Source.EVENT_CANCELLATION,
                )
            refunded += 1
        except NothingToRefundError:
            continue  # offline tier or nothing left — cancellation alone was correct
        except (RefundInsufficientBalanceError, StripeRefundFailed) as exc:
            # Durable failure record — the request-path rollback semantics don't apply here.
            Refund.objects.create(
                payment=payment,
                amount=refund_service.remaining_refundable(payment),
                currency=payment.currency,
                status=Refund.RefundStatus.FAILED,
                failure_reason=str(exc) or exc.__class__.__name__,
                initiated_by=initiator,
                reason="event_cancelled",
                source=Refund.Source.EVENT_CANCELLATION,
            )
            failed += 1

    summary = {"cancelled": cancelled, "refunded": refunded, "failed": failed}
    logger.info("event_refund_sweep_done", event_id=event_id, **summary)
    return summary
