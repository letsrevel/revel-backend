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
from events.tasks.attendees import build_attendee_visibility_flags

logger = structlog.get_logger(__name__)


@shared_task(name="events.refund_cancelled_event_tickets")
def refund_cancelled_event_tickets(event_id: str, initiated_by_id: str | None = None) -> dict[str, int]:
    """Fan out one ``refund_one_cancelled_event_ticket`` subtask per non-cancelled ticket.

    Stays cheap and Stripe-free — it only snapshots ticket ids and dispatches, matching
    the fan-out pattern used elsewhere (``tasks/recurrence.py``, ``tasks/invoicing.py``,
    ``tasks/waitlist.py``). Each subtask refunds-then-cancels one ticket independently,
    so a crashed/partial run can simply be re-dispatched (see
    ``event_update_service.update_status``, which always re-dispatches this task on a
    cancel-with-refund call) — there is no parent-level state to reconcile. Per-ticket
    outcomes live on ``Refund`` rows and ticket statuses; there is no live summary to
    return once the work is fanned out across independent tasks.
    """
    ticket_ids = list(
        Ticket.objects.filter(event_id=event_id)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("id", flat=True)
    )
    for ticket_id in ticket_ids:
        refund_one_cancelled_event_ticket.delay(str(ticket_id), initiated_by_id)

    logger.info("event_refund_sweep_dispatched", event_id=event_id, dispatched=len(ticket_ids))
    return {"dispatched": len(ticket_ids)}


@shared_task(name="events.refund_one_cancelled_event_ticket")
def refund_one_cancelled_event_ticket(ticket_id: str, initiated_by_id: str | None = None) -> None:
    """Refund, then cancel, a single ticket as part of a bulk event-cancellation sweep.

    The refund runs FIRST, in its own transaction; the cancellation (including flipping
    a PENDING payment to FAILED) runs SECOND, in its own transaction. This ordering is
    what makes a crash between the two steps safely re-runnable: the ticket is still
    non-CANCELLED, so re-dispatching re-enters this function, ``issue_refund`` finds
    ``remaining_refundable() == 0`` (the prior run's ``Refund`` row already covers the
    full amount) and raises ``NothingToRefundError``, and execution falls through to
    cancellation as normal. A crash mid-``stripe.Refund.create`` is covered by
    ``issue_refund``'s deterministic idempotency key (same payment/sequence/amount on
    retry). Cancellation proceeds regardless of whether the refund attempt succeeded,
    failed, or had nothing to do — a ticket must not survive a cancelled event just
    because Stripe declined the refund.

    Ticket-field mutations use a queryset ``.update()`` rather than ``instance.save()``
    so no ``post_save`` signal fires — attendees already get the single EVENT_CANCELLED
    blast (and organizers get Task 9's summary), so a per-ticket TICKET_CANCELLED
    notification here would be a storm. That bypass also skips
    ``events.signals.handle_ticket_visibility_and_potluck``, whose two jobs are handled
    separately: the potluck-unclaim is judged harmless to skip (the event is dead, its
    potluck is moot), but ``Event.attendee_count``/``is_full`` recompute is NOT optional
    — it's the only path that keeps those fields from freezing at their
    pre-cancellation value once the un-cancel guard makes the event unrecoverable — so
    ``build_attendee_visibility_flags`` is dispatched explicitly below, once the cancel
    transaction commits. It also deliberately skips
    ``notifications.signals.waitlist.handle_ticket_waitlist_logic``: the event is
    cancelled, so there is nothing left to reprocess on the waitlist.

    Already-cancelled tickets (a fully-completed prior run, or a ticket cancelled
    independently in the meantime) are a no-op.
    """
    from accounts.models import RevelUser

    initiator = RevelUser.objects.filter(pk=initiated_by_id).first() if initiated_by_id else None
    ticket = Ticket.objects.select_related("tier", "event").filter(pk=ticket_id).first()
    if ticket is None or ticket.status == Ticket.TicketStatus.CANCELLED:
        return

    payment = Payment.objects.filter(ticket=ticket).first()
    if payment is not None:
        try:
            with transaction.atomic():
                refund_service.issue_refund(
                    payment,
                    amount=None,
                    initiated_by=initiator,
                    reason="event_cancelled",
                    source=Refund.Source.EVENT_CANCELLATION,
                )
        except NothingToRefundError:
            pass  # offline tier, PENDING payment, or already fully refunded — cancel below regardless
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

    with transaction.atomic():
        locked_ticket = Ticket.objects.select_for_update().select_related("tier", "event").filter(pk=ticket_id).first()
        if locked_ticket is None or locked_ticket.status == Ticket.TicketStatus.CANCELLED:
            return
        locked_payment = Payment.objects.select_for_update().filter(ticket=locked_ticket).first()
        if locked_payment is not None and locked_payment.status == Payment.PaymentStatus.PENDING:
            locked_payment.status = Payment.PaymentStatus.FAILED
            locked_payment.save(update_fields=["status"])

        TicketTier.objects.filter(pk=locked_ticket.tier_id, quantity_sold__gt=0).update(
            quantity_sold=F("quantity_sold") - 1
        )
        Ticket.objects.filter(pk=locked_ticket.pk).update(
            status=Ticket.TicketStatus.CANCELLED,
            cancelled_at=timezone.now(),
            cancelled_by=initiator,
            cancellation_source=CancellationSource.EVENT_CANCELLATION,
            cancellation_reason=locked_ticket.event.cancellation_reason or "",
        )
        # The .update() above fired no post_save signal, so the usual
        # attendee_count/is_full recompute (events.signals
        # .handle_ticket_visibility_and_potluck) never ran — replicate it explicitly,
        # same per-ticket dispatch cost as the signal, and idempotent either way.
        event_id = str(locked_ticket.event_id)
        transaction.on_commit(lambda: build_attendee_visibility_flags.delay(event_id))
