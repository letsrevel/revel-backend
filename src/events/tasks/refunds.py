"""Bulk cancel-and-refund sweep for cancelled events."""

import typing as t
from datetime import timedelta

import stripe
import structlog
from celery import shared_task
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from events.exceptions import NothingToRefundError, RefundInsufficientBalanceError, StripeRefundFailed
from events.models import Event, Payment, Refund, Ticket, TicketTier
from events.models.ticket import CancellationSource
from events.service import day_membership_service, discount_code_service, refund_service, ticket_service
from events.tasks.attendees import build_attendee_visibility_flags
from notifications.signals.payment import send_event_refund_summary

logger = structlog.get_logger(__name__)

# Bound on send_event_refund_summary_task's self-rescheduling (30 * 120s = 1 hour).
_SUMMARY_MAX_ATTEMPTS = 30
_SUMMARY_RETRY_COUNTDOWN = 120


_TRANSIENT_STRIPE_ERRORS = (stripe.error.APIConnectionError, stripe.error.RateLimitError, stripe.error.APIError)
# After this long of transient failures, the refund is recorded as FAILED so the alert fires.
LATE_PAYMENT_TRANSIENT_GIVE_UP = timedelta(hours=6)


def _clear_late_payment_marker(payment_id: str) -> None:
    """End a "refund due" marker that has nothing to refund, so the sweep stops matching it."""
    live_refund = Refund.objects.filter(payment=OuterRef("pk")).exclude(status=Refund.RefundStatus.FAILED)
    Payment.objects.filter(~Exists(live_refund), pk=payment_id, refund_status=Payment.RefundStatus.PENDING).update(
        refund_status=None
    )


@shared_task(name="events.refund_late_payment")
def refund_late_payment(payment_id: str) -> None:
    """Refund a payment that completed after its ticket was cancelled (#1459).

    Idempotent: it acts only on the "refund due" marker (SUCCEEDED, ``refund_status`` PENDING, no live
    ``Refund`` row) for a ticket that is still CANCELLED, all read under the Payment lock. The full amount
    is refunded and the platform fee is kept, as for every refund. Every branch ends the marker or keeps it
    on purpose:

    - nothing to refund, or the ticket is no longer cancelled: the marker is cleared;
    - a transient Stripe error: the marker stays, and ``cleanup_expired_payments`` retries later, for up to
      ``LATE_PAYMENT_TRANSIENT_GIVE_UP`` after the marker was set;
    - any other Stripe failure, or a transient one past that limit: a FAILED ``Refund`` row,
      ``refund_status`` FAILED, and the error log ``late_payment_refund_failed`` for the alert.

    A refund the organizer issued by hand (a live ``Refund`` row, also a partial one) ends the automatic
    refund: the organizer took over the payment, and a second refund could double-refund the buyer.
    """
    try:
        with transaction.atomic():
            payment = (
                Payment.objects.select_for_update(of=("self",))
                .filter(
                    pk=payment_id, status=Payment.PaymentStatus.SUCCEEDED, refund_status=Payment.RefundStatus.PENDING
                )
                .first()
            )
            if payment is None or payment.refunds.exclude(status=Refund.RefundStatus.FAILED).exists():
                return  # not due: refunded, in flight, or not a late payment
            if not Ticket.objects.filter(pk=payment.ticket_id, status=Ticket.TicketStatus.CANCELLED).exists():
                _clear_late_payment_marker(payment_id)
                logger.warning("late_payment_refund_skipped_ticket_not_cancelled", payment_id=payment_id)
                return
            refund_service.issue_refund(
                payment,
                amount=None,
                initiated_by=None,
                reason="late_payment",
                source=Refund.Source.LATE_PAYMENT,
            )
    except NothingToRefundError:
        _clear_late_payment_marker(payment_id)
        logger.info("late_payment_refund_nothing_to_refund", payment_id=payment_id)
    except (RefundInsufficientBalanceError, StripeRefundFailed) as exc:
        marked_at = Payment.objects.filter(pk=payment_id).values_list("updated_at", flat=True).first()
        still_retrying = marked_at is not None and timezone.now() - marked_at < LATE_PAYMENT_TRANSIENT_GIVE_UP
        if isinstance(exc.__cause__, _TRANSIENT_STRIPE_ERRORS) and still_retrying:
            logger.warning("late_payment_refund_retry_later", payment_id=payment_id, error=str(exc))
            return
        _record_late_payment_failure(payment_id, str(exc) or exc.__class__.__name__)


def _record_late_payment_failure(payment_id: str, reason: str) -> None:
    """Record a failed late-payment refund, unless another attempt moved the payment on meanwhile.

    The failed attempt released the Payment lock, so the marker is read again under a new lock.
    """
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update(of=("self",))
            .filter(pk=payment_id, status=Payment.PaymentStatus.SUCCEEDED, refund_status=Payment.RefundStatus.PENDING)
            .first()
        )
        if payment is None or payment.refunds.exclude(status=Refund.RefundStatus.FAILED).exists():
            logger.info("late_payment_refund_failure_superseded", payment_id=payment_id, error=reason)
            return
        Refund.objects.create(
            payment=payment,
            amount=refund_service.remaining_refundable(payment),
            currency=payment.currency,
            status=Refund.RefundStatus.FAILED,
            failure_reason=reason,
            reason="late_payment",
            source=Refund.Source.LATE_PAYMENT,
        )
        Payment.objects.filter(pk=payment.pk).update(
            refund_status=Payment.RefundStatus.FAILED, refund_failure_reason=reason
        )
    logger.error("late_payment_refund_failed", payment_id=payment_id, error=reason)


# Bounded backoff for a transient Stripe error: 1, 2, 4, 8, 16 and 32 minutes (about one hour in total).
ORPHANED_CHECKOUT_REFUND_MAX_RETRIES = 6
_ORPHANED_CHECKOUT_REFUND_BACKOFF_SECONDS = 60


@shared_task(
    bind=True, name="events.refund_orphaned_checkout_session", max_retries=ORPHANED_CHECKOUT_REFUND_MAX_RETRIES
)
def refund_orphaned_checkout_session(
    self: t.Any, session_id: str, payment_intent_id: str, stripe_account: str, organization_id: str
) -> None:
    """Refund in full a completed ticket or series-pass checkout session that has no Payment rows (#1489).

    The webhook queues this after commit, so no row lock is held across the Stripe call. The idempotency key
    is derived from the session id: a retry or a second dispatch never refunds twice, and after Stripe's 24-hour
    key window a second full refund fails with ``charge_already_refunded``, which is a no-op here. A transient
    Stripe error (connection, rate limit, 5xx) is retried with backoff. Any other Stripe error, or a transient
    one after the last retry, logs ``orphaned_checkout_refund_failed`` at ERROR for a manual refund. The
    created refund's status decides the log event, at ERROR so staff see each automatic refund:
    ``succeeded`` logs ``orphaned_checkout_refunded``; ``failed`` or ``canceled`` logs
    ``orphaned_checkout_refund_failed``; any other status (``pending``, ``requires_action``) logs
    ``orphaned_checkout_refund_pending``, and a later failure arrives through the ``refund.failed`` /
    ``refund.updated`` webhook. Ids only, no buyer data.

    Args:
        self: Celery task instance (automatically passed when bind=True).
        session_id: The completed Stripe Checkout Session.
        payment_intent_id: The PaymentIntent to refund.
        stripe_account: The connected account of the session, or "" for the platform account.
        organization_id: The organization that owns the Stripe account (for the log only).
    """
    ids = {"session_id": session_id, "payment_intent_id": payment_intent_id, "organization_id": organization_id}
    try:
        refund = refund_service.refund_orphaned_checkout_session(
            session_id=session_id, payment_intent_id=payment_intent_id, stripe_account=stripe_account
        )
    except stripe.error.StripeError as exc:
        if getattr(exc, "code", None) == "charge_already_refunded":
            logger.info("orphaned_checkout_refund_already_refunded", **ids)
            return
        if isinstance(exc, _TRANSIENT_STRIPE_ERRORS) and self.request.retries < ORPHANED_CHECKOUT_REFUND_MAX_RETRIES:
            logger.warning("orphaned_checkout_refund_retry", attempt=self.request.retries + 1, error=str(exc), **ids)
            raise self.retry(exc=exc, countdown=_ORPHANED_CHECKOUT_REFUND_BACKOFF_SECONDS * 2**self.request.retries)
        logger.error("orphaned_checkout_refund_failed", code=getattr(exc, "code", None), error=str(exc), **ids)
        return
    status = refund.status
    if status == "succeeded":
        log_event = "orphaned_checkout_refunded"
    elif status in ("failed", "canceled"):
        log_event = "orphaned_checkout_refund_failed"
    else:
        log_event = "orphaned_checkout_refund_pending"
    logger.error(log_event, refund_id=refund.id, status=status, **ids)


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

    Once dispatched, schedules ``send_event_refund_summary_task`` (delayed by
    ``_SUMMARY_RETRY_COUNTDOWN`` to give the fan-out a head start) to poll DB state and
    notify org staff when the sweep completes. Skipped when there is nothing to
    dispatch — an empty sweep has nothing to summarize.
    """
    ticket_ids = list(
        Ticket.objects.filter(event_id=event_id)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("id", flat=True)
    )
    for ticket_id in ticket_ids:
        refund_one_cancelled_event_ticket.delay(str(ticket_id), initiated_by_id)

    if ticket_ids:
        send_event_refund_summary_task.apply_async(args=(event_id,), countdown=_SUMMARY_RETRY_COUNTDOWN)

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
        # Lock order: Payment -> TicketTier -> Ticket (docs/engineering-notes.md).
        locked_payment = Payment.objects.select_for_update(of=("self",)).filter(ticket_id=ticket_id).first()
        locked_tickets = ticket_service.lock_tier_then_ticket(
            Ticket.objects.filter(pk=ticket_id).select_related("tier", "event")
        )
        locked_ticket = locked_tickets[0] if locked_tickets else None
        if locked_ticket is None or locked_ticket.status == Ticket.TicketStatus.CANCELLED:
            return
        if locked_payment is not None and locked_payment.status == Payment.PaymentStatus.PENDING:
            locked_payment.status = Payment.PaymentStatus.FAILED
            locked_payment.save(update_fields=["status"])

        TicketTier.objects.filter(pk=locked_ticket.tier_id, quantity_sold__gt=0).update(
            quantity_sold=F("quantity_sold") - 1
        )
        ticket_service.record_offline_payment_kept(locked_ticket)
        previous_status = locked_ticket.status
        if previous_status == Ticket.TicketStatus.PENDING:
            # Unfinished checkout: give the code's use back (after the tier lock). A confirmed ticket keeps it.
            discount_code_service.release_discount_uses([locked_ticket.discount_code_id])
        Ticket.objects.filter(pk=locked_ticket.pk).update(
            status=Ticket.TicketStatus.CANCELLED,
            offline_refund_amount=locked_ticket.offline_refund_amount,
            cancelled_at=timezone.now(),
            cancelled_by=initiator,
            cancellation_source=CancellationSource.EVENT_CANCELLATION,
            cancellation_reason=locked_ticket.event.cancellation_reason or "",
        )
        locked_ticket.status = Ticket.TicketStatus.CANCELLED
        # A checkout that completed between the refund step above and this lock was confirmed as a normal
        # sale; its ticket is now cancelled, so its money is refunded like any late payment (#1459).
        refund_service.queue_late_payment_refund_if_captured(locked_payment)
        # A queued subtask may run after the event started; what counts is whether the event was cancelled
        # (the refund sweep started, ``tickets_refund_started_at``) before it started.
        event = locked_ticket.event
        day_membership_service.on_ticket_status_change(
            locked_ticket,
            previous_status,
            revoke_ignoring_start=(event.tickets_refund_started_at or timezone.now()) < event.start,
        )
        # The .update() above fired no post_save signal, so the usual
        # attendee_count/is_full recompute (events.signals
        # .handle_ticket_visibility_and_potluck) never ran — replicate it explicitly,
        # same per-ticket dispatch cost as the signal, and idempotent either way.
        event_id = str(locked_ticket.event_id)
        transaction.on_commit(lambda: build_attendee_visibility_flags.delay(event_id))


@shared_task(name="events.send_event_refund_summary")
def send_event_refund_summary_task(event_id: str, attempt: int = 0) -> None:
    """Poll DB state for the fan-out sweep's completion, then notify org staff.

    Named ``..._task`` in code (registered as ``events.send_event_refund_summary``) to
    keep it distinct from ``notifications.signals.payment.send_event_refund_summary``,
    the notification-dispatch function this calls once the sweep is done — the two
    share a concept but not a job.

    There is no single point that knows when the fan-out from
    ``refund_cancelled_event_tickets`` finishes (each ticket refunds/cancels
    independently), so this re-schedules itself every ``_SUMMARY_RETRY_COUNTDOWN``
    seconds, checking ticket state directly, until either every ticket on the event is
    CANCELLED or ``_SUMMARY_MAX_ATTEMPTS`` is reached. Giving up still sends the
    summary — with ``still_active`` counting whatever never finished — rather than
    silently dropping the notification.
    """
    if (
        Ticket.objects.filter(event_id=event_id).exclude(status=Ticket.TicketStatus.CANCELLED).exists()
        and attempt < _SUMMARY_MAX_ATTEMPTS
    ):
        send_event_refund_summary_task.apply_async(args=(event_id, attempt + 1), countdown=_SUMMARY_RETRY_COUNTDOWN)
        return

    event = Event.objects.select_related("organization").filter(pk=event_id).first()
    if event is None:  # pragma: no cover - event deletion is not a supported flow
        return

    cancelled = Ticket.objects.filter(
        event_id=event_id, cancellation_source=CancellationSource.EVENT_CANCELLATION
    ).count()
    still_active = Ticket.objects.filter(event_id=event_id).exclude(status=Ticket.TicketStatus.CANCELLED).count()

    sweep_refunds = Refund.objects.filter(source=Refund.Source.EVENT_CANCELLATION, payment__ticket__event_id=event_id)
    # Distinct payment_ids, matching `failed`'s unit below — a payment retried after a
    # FAILED attempt leaves two rows (FAILED + PENDING/SUCCEEDED) and must count once.
    refunded = (
        sweep_refunds.filter(status__in=[Refund.RefundStatus.PENDING, Refund.RefundStatus.SUCCEEDED])
        .values("payment_id")
        .distinct()
        .count()
    )
    # A payment counts as failed only if ALL of its sweep refund rows are FAILED — a
    # retried-and-succeeded refund is not a failure, even though it also left a FAILED row.
    failed = (
        sweep_refunds.values("payment_id")
        .annotate(non_failed_count=Count("id", filter=~Q(status=Refund.RefundStatus.FAILED)))
        .filter(non_failed_count=0)
        .count()
    )

    send_event_refund_summary(
        event=event,
        cancelled=cancelled,
        refunded=refunded,
        failed=failed,
        still_active=still_active,
    )
