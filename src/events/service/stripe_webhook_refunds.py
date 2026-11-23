"""Ticket-refund handlers for the Stripe webhook dispatcher.

Mixin methods for :class:`events.service.stripe_webhooks.StripeEventHandler`,
split out to keep that module under the file-length cap. The dispatch map,
dedup gate, and ``_resolve_refunds`` (shared with the subscription mixin) stay
in ``stripe_webhooks``; these methods match each ``charge.refunded`` refund
object to its ticket Payment(s) and apply it, record-only — a refund never
cancels a ticket or reclaims tier capacity, since cancel and refund are
orthogonal operations.
"""

import typing as t
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import stripe
import structlog
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from events.models import MembershipPayment, Payment, Refund, Ticket
from events.models.attendee_invoice import AttendeeInvoiceCreditNote
from events.service import refund_service
from events.utils.currency import from_stripe_amount, to_stripe_amount
from events.utils.erased_holder import holder_email, stripe_payload_to_store
from notifications.signals.payment import send_refund_failed, send_refund_unmatched, send_ticket_refund_failed
from notifications.signals.ticket import staff_holder_name

logger = structlog.get_logger(__name__)


def _distinct_amounts(payments: list[Payment]) -> set[tuple[int, str]]:
    """Return the distinct (smallest-unit gross amount, currency) pairs across payments.

    Used only for the unmatched-refund notification's ``reason`` field (informational).
    Matching itself compares remaining-refundable balances — see
    :func:`_distinct_remaining_amounts`.
    """
    return {(to_stripe_amount(p.amount, p.currency), p.currency) for p in payments}


def _distinct_remaining_amounts(payments: list[Payment]) -> set[tuple[int, str]]:
    """Return the distinct (smallest-unit remaining-refundable amount, currency) pairs.

    A prior partial refund shrinks a payment's remaining balance below its gross
    amount — matching must compare what is still owed, not what was originally
    charged, or a legitimate remaining-balance match gets refused as "non-uniform".
    """
    return {(to_stripe_amount(refund_service.remaining_refundable(p), p.currency), p.currency) for p in payments}


# Stripe refund status -> the status of our Refund row. Only ``succeeded`` is money returned to the buyer.
_STRIPE_REFUND_OUTCOMES: dict[str, Refund.RefundStatus] = {
    "succeeded": Refund.RefundStatus.SUCCEEDED,
    "pending": Refund.RefundStatus.PENDING,
    "requires_action": Refund.RefundStatus.PENDING,
    "failed": Refund.RefundStatus.FAILED,
    "canceled": Refund.RefundStatus.FAILED,
}


def _stripe_refund_outcome(refund: dict[str, t.Any]) -> Refund.RefundStatus:
    """Map the Stripe refund ``status`` to the status of our Refund row.

    A missing or unknown status counts as pending, never as succeeded: Revel must not
    treat money as returned that Stripe did not confirm. The next ``refund.updated``
    event settles it. The warning makes a payload without the field visible.
    """
    status = refund.get("status")
    outcome = _STRIPE_REFUND_OUTCOMES.get(status) if isinstance(status, str) else None
    if outcome is None:
        logger.warning(
            "stripe_refund_status_missing" if status is None else "stripe_refund_status_unknown",
            refund_id=refund.get("id"),
            status=status,
        )
        return Refund.RefundStatus.PENDING
    return outcome


def _transition_refund_row(row: Refund, outcome: Refund.RefundStatus, failure_reason: str, refund_id: str) -> bool:
    """Move a Refund row along its state machine; return True when the status changed.

    - PENDING -> SUCCEEDED (stamps ``succeeded_at`` once).
    - PENDING -> FAILED and SUCCEEDED -> FAILED (records ``failure_reason``).
    - Everything else is a no-op. A row never goes back to PENDING, and FAILED is final,
      so a stale or reordered event cannot undo a later one.

    A blank ``stripe_refund_id`` is filled in even when the status does not change.
    """
    changed = True
    if outcome == Refund.RefundStatus.SUCCEEDED and row.status == Refund.RefundStatus.PENDING:
        row.status = Refund.RefundStatus.SUCCEEDED
        row.failure_reason = ""
        if row.succeeded_at is None:  # period attribution for revenue reports, never re-stamped on replay
            row.succeeded_at = timezone.now()
    elif outcome == Refund.RefundStatus.FAILED and row.status in (
        Refund.RefundStatus.PENDING,
        Refund.RefundStatus.SUCCEEDED,
    ):
        row.status = Refund.RefundStatus.FAILED
        row.failure_reason = failure_reason
        row.succeeded_at = None
    else:
        changed = False
    if changed or not row.stripe_refund_id:
        row.stripe_refund_id = row.stripe_refund_id or refund_id
        row.save(update_fields=["status", "failure_reason", "succeeded_at", "stripe_refund_id", "updated_at"])
    return changed


@dataclass
class FailedRefundRow:
    """A Refund row that this delivery moved to FAILED (input for the alerts)."""

    row: Refund
    payment: Payment
    refund: dict[str, t.Any]
    previous_row_status: str
    previous_payment_status: str


@dataclass
class ProcessedRefunds:
    """What :meth:`TicketRefundHandlersMixin._process_refunds` changed.

    Attributes:
        newly_refunded_ids: Payment ids with a Refund row that became SUCCEEDED in this call.
        newly_refund_row_ids: The pks of those Refund rows.
        touched_session_id: The session id of the last such Payment, or None.
        failures: The Refund rows that this call moved to FAILED.
    """

    newly_refunded_ids: list[str] = field(default_factory=list)
    newly_refund_row_ids: list[str] = field(default_factory=list)
    touched_session_id: str | None = None
    failures: list[FailedRefundRow] = field(default_factory=list)


@dataclass
class _RefundBatch:
    """Working state of one :meth:`TicketRefundHandlersMixin._process_refunds` call."""

    by_id: dict[uuid.UUID, Payment]
    previous_status: dict[uuid.UUID, str]
    result: ProcessedRefunds = field(default_factory=ProcessedRefunds)
    touched: dict[uuid.UUID, str] = field(default_factory=dict)  # payment pk -> id of the last refund that touched it
    reopen: set[uuid.UUID] = field(default_factory=set)  # payments with a row that this call moved to FAILED
    succeeded_payment_ids: set[uuid.UUID] = field(default_factory=set)

    def row_became_succeeded(self, payment: Payment, row: Refund, refund_id: str) -> None:
        """Record that ``row`` of ``payment`` is now SUCCEEDED."""
        self.touched[payment.pk] = refund_id
        self.succeeded_payment_ids.add(payment.pk)
        self.result.newly_refund_row_ids.append(str(row.pk))


def _safe_uuid_list(value: str) -> list[uuid.UUID]:
    """Parse a metadata-supplied uuid string, returning [] on garbage instead of raising."""
    try:
        return [uuid.UUID(value)]
    except ValueError, AttributeError:
        return []


class TicketRefundHandlersMixin:
    """Ticket-refund webhook handlers (record-only, #865)."""

    if t.TYPE_CHECKING:
        # Provided by the host StripeEventHandler; declared for mypy only.
        event: stripe.Event

        def _resolve_refunds(self, charge_data: dict[str, t.Any]) -> list[dict[str, t.Any]]: ...

        def handle_refund_failure(self, event: stripe.Event) -> None:
            """Log the orphaned-checkout refund alert (host handler)."""

        def _handle_subscription_refund_object(self, event: stripe.Event, payment_intent_id: str) -> None: ...

    @transaction.atomic
    def handle_refund_event(self, event: stripe.Event) -> None:
        """Apply a ``refund.updated`` or ``refund.failed`` event to the refund it describes (#1507).

        ``charge.refunded`` carries the status of the refunds when it fires, but a refund settles
        later: ``pending`` becomes ``succeeded``, or ``failed`` / ``canceled``. These events move
        the Refund rows along the same code path as ``charge.refunded``.

        The orphaned-checkout alert (:meth:`handle_refund_failure`, #1489) runs first and is
        unchanged. It only acts on a payment intent that has no Payment row.
        """
        self.handle_refund_failure(event)
        refund = event.data.object
        payment_intent_id = refund.get("payment_intent")
        if not payment_intent_id:
            return
        # Stripe payment intents are per purchase: a MembershipPayment on it means a subscription invoice.
        if MembershipPayment.objects.filter(stripe_payment_intent_id=payment_intent_id).exists():
            self._handle_subscription_refund_object(event, payment_intent_id)
            return
        self._handle_refund_object(event, payment_intent_id)

    def _handle_refund_object(self, event: stripe.Event, payment_intent_id: str) -> None:
        """Apply the refund object of a ``refund.*`` event to the ticket Payments of its intent.

        The event carries the refund itself, so no Stripe call is needed. The unmatched-refund
        notification stays with ``charge.refunded``: an ambiguous refund here is only logged.
        """
        # Cheap unlocked probe: an automatic refund of an orphaned session has no Payment row.
        if not Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).exists():
            logger.debug("stripe_refund_event_unknown_intent", payment_intent_id=payment_intent_id)
            return
        self._lock_and_process_refunds(
            refunds=[event.data.object],
            payment_intent_id=payment_intent_id,
            raw_response=dict(event),
            notify_unmatched=False,
            retry_duplicates=False,
        )

    def _handle_ticket_refunds(self, event: stripe.Event, payment_intent_id: str) -> None:
        """Match each refund object in the charge to its specific ticket Payment row.

        Record-only: applying a match never cancels a ticket or reclaims tier
        capacity (see :meth:`_process_refunds`) — cancel and refund are
        orthogonal operations. A refund counts as money returned only when its
        Stripe status is ``succeeded`` (#1507).

        Matching strategy (first match wins):
          1. existing stripe_refund_id on a Payment or Refund row
          1.5. refund.metadata["refund_id"] — our own Refund row pointer
          2. refund.metadata["ticket_id"]
          2.5. exactly one Payment on the intent — unambiguous by construction
          3. exactly one unrefunded Payment whose remaining refundable balance matches
             the refund amount, *and* the intent's Payments are uniform in remaining
             balance (otherwise the match is a guess)
          4. refund.amount equals the sum of unrefunded payments' remaining refundable
             balances (full remaining batch) — each payment is allocated its own
             remaining, not the aggregate or its gross amount
          5. ambiguous → logged, no mutation
        """
        charge_data = event.data.object

        # Cheap unlocked probe so unknown intents bail before any outbound call.
        if not Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).exists():
            logger.warning("stripe_refund_unknown_intent", payment_intent_id=payment_intent_id)
            return

        # Resolve refunds BEFORE taking row locks: _resolve_refunds may make an
        # outbound Stripe call, and holding select_for_update locks across that
        # network round-trip would block concurrent user-initiated cancels
        # (cancellation_service locks the same Payment rows) for its duration.
        refunds = self._resolve_refunds(charge_data)
        if not refunds:
            logger.warning("stripe_refund_event_no_refund_data", payment_intent_id=payment_intent_id)
            return

        self._lock_and_process_refunds(
            refunds=refunds,
            payment_intent_id=payment_intent_id,
            raw_response=dict(event),
            notify_unmatched=True,
            retry_duplicates=True,
        )

    def _lock_and_process_refunds(
        self,
        *,
        refunds: list[dict[str, t.Any]],
        payment_intent_id: str,
        raw_response: dict[str, t.Any],
        notify_unmatched: bool,
        retry_duplicates: bool,
    ) -> None:
        """Lock the intent's Payment rows, apply the refunds, and schedule the credit note.

        The refunds are already resolved, so no outbound Stripe call runs under the lock.

        Args:
            refunds: The Stripe refund object dicts to apply.
            payment_intent_id: The Stripe payment intent id.
            raw_response: The full serialised webhook event (for audit).
            notify_unmatched: Whether an ambiguous refund raises the unmatched-refund notification.
            retry_duplicates: Whether a delivery that changes nothing re-enqueues the credit note.
        """
        # Lock Payment rows for the duration of this transaction. Stripe webhooks
        # are at-least-once, and a Stripe-Dashboard refund's webhook can also race
        # against an in-flight user-initiated cancel (which itself locks the same
        # Payment via cancellation_service). Without the lock here, two concurrent
        # transactions could both apply the same refund twice. Locking with
        # `of=("self",)` keeps the lock scoped to Payment rows so we don't also
        # block concurrent purchases that need to lock the joined Tier.
        candidates = list(
            Payment.objects.select_for_update(of=("self",))
            .filter(stripe_payment_intent_id=payment_intent_id)
            .order_by("pk")
        )
        if not candidates:
            logger.warning("stripe_refund_unknown_intent", payment_intent_id=payment_intent_id)
            return

        processed = self._process_refunds(
            refunds=refunds,
            candidates=candidates,
            raw_response=raw_response,
            payment_intent_id=payment_intent_id,
            notify_unmatched=notify_unmatched,
        )

        if processed.newly_refunded_ids or retry_duplicates:
            self._schedule_credit_note(
                payment_intent_id=payment_intent_id,
                candidates=candidates,
                newly_refunded_ids=processed.newly_refunded_ids,
                newly_refund_row_ids=processed.newly_refund_row_ids,
                touched_session_id=processed.touched_session_id,
            )

        logger.info(
            "stripe_refund_processed",
            payment_intent_id=payment_intent_id,
            refund_count=len(refunds),
            newly_refunded_payment_ids=processed.newly_refunded_ids,
        )

    @staticmethod
    def _existing_refund_rows(refund: dict[str, t.Any], candidates: list[Payment]) -> list[Refund]:
        """Return the Refund rows of the candidates that already belong to this Stripe refund.

        A row belongs to it when it carries the Stripe refund id (a replay, or one row of a
        batch refund) or when the refund metadata points at it (``refund_id``, set on every
        refund issued through Revel). One query, whatever the number of candidates.
        """
        refund_id: str | None = refund.get("id")
        match = Q(pk__in=_safe_uuid_list((refund.get("metadata") or {}).get("refund_id") or ""))
        if refund_id:
            match |= Q(stripe_refund_id=refund_id)
        return list(Refund.objects.filter(match, payment__in=candidates).order_by("created_at", "pk"))

    def _process_refunds(
        self,
        *,
        refunds: list[dict[str, t.Any]],
        candidates: list[Payment],
        raw_response: dict[str, t.Any],
        payment_intent_id: str,
        notify_unmatched: bool = True,
    ) -> ProcessedRefunds:
        """Match each refund to its Payment(s) and apply its Stripe status.

        Record-only: a refund never cancels a ticket or releases tier capacity.

        For each refund, in this order:

        1. A refund that already has Refund rows (by Stripe id or metadata pointer, fan-out
           safe) moves those rows along :func:`_transition_refund_row`.
        2. A refund with no row and a ``succeeded`` or ``pending`` status is matched to
           its Payment(s) and gets a new row with that status.
        3. A ``failed`` refund with no row changes nothing (it never counted).
        4. An ambiguous refund changes nothing; ``charge.refunded`` raises the unmatched notification.

        The rows that exist move first and their Payment totals are synced; only then are the refunds
        with no row matched, so a refund that failed in this batch has reopened its Payment by then.
        The totals are synced again after the new rows.

        Args:
            refunds: The refund object dicts (from ``_resolve_refunds`` or the refund event).
            candidates: All locked Payment rows for this intent.
            raw_response: The full serialised webhook event (for audit).
            payment_intent_id: Stripe payment intent id (logging only).
            notify_unmatched: Whether an ambiguous refund raises the unmatched-refund notification.

        Returns:
            What changed: see :class:`ProcessedRefunds`.
        """
        batch = _RefundBatch(by_id={p.pk: p for p in candidates}, previous_status={p.pk: p.status for p in candidates})

        # Pass 1: move the rows that exist. Their totals are synced before any new row is matched, so
        # a refund that failed in this batch has already reopened its Payment when the next one matches.
        new_refunds: list[tuple[dict[str, t.Any], Refund.RefundStatus]] = []
        for refund in refunds:
            outcome = _stripe_refund_outcome(refund)
            rows = self._existing_refund_rows(refund, candidates)
            if rows:
                self._transition_existing_rows(batch, rows, refund, outcome)
            else:
                new_refunds.append((refund, outcome))
        self._sync_batch_totals(batch, raw_response)

        # Pass 2: the refunds with no row yet.
        for refund, outcome in new_refunds:
            refund_id = str(refund.get("id") or "")
            if outcome == Refund.RefundStatus.FAILED:
                # ponytail: no money moved, so Revel records nothing for a failed refund it has no row for; a
                # late charge.refunded that carries the failed status records nothing either. Upgrade path: a
                # table keyed by stripe_refund_id, if an audit of unknown refunds is ever needed.
                logger.info(
                    "stripe_refund_failed_unknown",
                    payment_intent_id=payment_intent_id,
                    refund_id=refund_id,
                    status=refund.get("status"),
                )
                continue

            matched = self._match_refund_to_payments(refund, candidates)
            if not matched:
                logger.warning(
                    "stripe_refund_ambiguous_match",
                    payment_intent_id=payment_intent_id,
                    refund_id=refund.get("id"),
                    refund_amount=refund.get("amount"),
                    candidate_payment_ids=[str(c.id) for c in candidates if c.status != Payment.PaymentStatus.REFUNDED],
                    candidate_amounts=[
                        f"{c.amount}{c.currency}" for c in candidates if c.status != Payment.PaymentStatus.REFUNDED
                    ],
                )
                if notify_unmatched:
                    self._notify_unmatched_refund(refund, candidates, payment_intent_id)
                continue
            self._create_refund_rows(batch, matched, refund, outcome)
        self._sync_batch_totals(batch, raw_response)

        result = batch.result
        self._report_failed_refunds(result.failures, payment_intent_id)
        return result

    def _sync_batch_totals(self, batch: _RefundBatch, raw_response: dict[str, t.Any]) -> None:
        """Recompute the totals of the Payments touched since the last call, then reset the batch marks."""
        for payment_pk, last_refund_id in batch.touched.items():
            payment = batch.by_id[payment_pk]
            self._sync_payment_refund_totals(
                payment, raw_response, refund_id=last_refund_id, may_reopen=payment_pk in batch.reopen
            )
            if payment_pk in batch.succeeded_payment_ids:
                if str(payment.id) not in batch.result.newly_refunded_ids:
                    batch.result.newly_refunded_ids.append(str(payment.id))
                batch.result.touched_session_id = payment.stripe_session_id
        batch.touched.clear()
        batch.reopen.clear()
        batch.succeeded_payment_ids.clear()

    @staticmethod
    def _transition_existing_rows(
        batch: _RefundBatch, rows: list[Refund], refund: dict[str, t.Any], outcome: Refund.RefundStatus
    ) -> None:
        """Move the existing Refund rows of ``refund`` to its Stripe status; record what changed."""
        refund_id = str(refund.get("id") or "")
        failure_reason = str(refund.get("failure_reason") or "")
        for row in rows:
            before = row.status
            if not _transition_refund_row(row, outcome, failure_reason, refund_id):
                continue
            payment = batch.by_id[row.payment_id]
            if row.status == Refund.RefundStatus.SUCCEEDED:
                batch.row_became_succeeded(payment, row, refund_id)
                continue
            batch.touched[payment.pk] = refund_id
            batch.reopen.add(payment.pk)
            batch.result.failures.append(
                FailedRefundRow(
                    row=row,
                    payment=payment,
                    refund=refund,
                    previous_row_status=before,
                    previous_payment_status=batch.previous_status[payment.pk],
                )
            )

    @staticmethod
    def _create_refund_rows(
        batch: _RefundBatch, matched: list[Payment], refund: dict[str, t.Any], outcome: Refund.RefundStatus
    ) -> None:
        """Create a Refund row with the Stripe status for each Payment the refund matched."""
        refund_id = str(refund.get("id") or "")
        # Branch 4 fans out a single refund across N Payments — each gets its
        # own remaining refundable amount, not the aggregate and not its gross
        # amount. Branches 1-3 always return one row.
        is_full_batch = len(matched) > 1
        for payment in matched:
            allocated_amount = (
                refund_service.remaining_refundable(payment)
                if is_full_batch
                else from_stripe_amount(int(refund.get("amount", 0)), payment.currency)
            )
            row = Refund(
                payment=payment,
                amount=allocated_amount,
                currency=payment.currency,
                source=Refund.Source.STRIPE_DASHBOARD,
                stripe_refund_id=refund_id,
                status=outcome,
                succeeded_at=timezone.now() if outcome == Refund.RefundStatus.SUCCEEDED else None,
            )
            # ponytail: a stale embedded charge.refunded (unpinned endpoints only) can create a PENDING row for a
            # refund whose final refund.* event was already consumed; that row never settles. Upgrade path: a
            # sweep that re-checks old PENDING rows with Stripe.
            row.save()
            if outcome == Refund.RefundStatus.SUCCEEDED:
                batch.row_became_succeeded(payment, row, refund_id)
            else:
                batch.touched[payment.pk] = refund_id

    @staticmethod
    def _report_failed_refunds(failures: list[FailedRefundRow], payment_intent_id: str) -> None:
        """Alert for each Refund row that this delivery moved to FAILED (#1507).

        - An ERROR log ``ticket_refund_failed`` per row (ids only), for the Loki ruler alert.
        - One staff notification per Stripe refund: the owners and the staff with ``manage_tickets``
          learn that the buyer did not get the money back.
        - A buyer notification per Payment that was REFUNDED before this delivery and is not REFUNDED
          at the end of it: the buyer got a "ticket refunded" email, so the buyer now hears that it
          failed, once per Payment, for the sum of its failed refunds. A new refund in the same batch can
          refund the Payment again; then the buyer hears nothing.

        Revel does not reverse an issued credit note. The staff notification says so.
        Runs inside the webhook's atomic block, like the unmatched-refund notification.
        """
        if not failures:
            return
        tickets = Ticket.objects.select_related("event__organization", "user").in_bulk(
            [f.payment.ticket_id for f in failures]
        )
        credit_notes: dict[uuid.UUID, list[str]] = {}
        for row_id, number in AttendeeInvoiceCreditNote.objects.filter(
            refunds__in=[f.row for f in failures]
        ).values_list("refunds", "credit_note_number"):
            credit_notes.setdefault(row_id, []).append(number)

        staff_groups: dict[str, list[FailedRefundRow]] = {}
        buyer_corrections: dict[uuid.UUID, list[FailedRefundRow]] = {}
        for failure in failures:
            ticket = tickets[failure.payment.ticket_id]
            logger.error(
                "ticket_refund_failed",
                payment_id=str(failure.payment.id),
                payment_intent_id=payment_intent_id,
                organization_id=str(ticket.event.organization_id),
                event_id=str(ticket.event_id),
                ticket_id=str(ticket.id),
                refund_id=failure.refund.get("id"),
                status=failure.refund.get("status"),
                failure_reason=failure.refund.get("failure_reason"),
                previous_status=failure.previous_row_status,
                credit_note_issued=failure.row.pk in credit_notes,
            )
            staff_groups.setdefault(str(failure.refund.get("id") or ""), []).append(failure)
            # A later refund of the same batch may have refunded the Payment again: then no failure to report.
            if (
                failure.previous_payment_status == Payment.PaymentStatus.REFUNDED
                and failure.payment.status != Payment.PaymentStatus.REFUNDED
            ):
                buyer_corrections.setdefault(failure.payment.pk, []).append(failure)

        # One message per reopened Payment, whatever the number of refunds that stopped counting.
        for group in buyer_corrections.values():
            send_ticket_refund_failed(
                payment=group[0].payment, refund_amount=sum((f.row.amount for f in group), Decimal("0"))
            )

        for refund_id, group in staff_groups.items():
            first = group[0]
            ticket = tickets[first.payment.ticket_id]
            send_refund_failed(
                organization=ticket.event.organization,
                refund_id=refund_id,
                payment_intent_id=payment_intent_id,
                refund_amount=sum((f.row.amount for f in group), Decimal("0")),
                currency=first.row.currency,
                failure_reason=first.row.failure_reason,
                holder_name=staff_holder_name(ticket),
                holder_email=holder_email(ticket),
                ticket=ticket,
                credit_note_numbers=sorted({n for f in group for n in credit_notes.get(f.row.pk, [])}),
            )

    def _notify_unmatched_refund(
        self, refund: dict[str, t.Any], candidates: list[Payment], payment_intent_id: str
    ) -> None:
        """Raise a durable staff notification for a refund the matcher declined.

        Covers both decline paths — the non-uniform-batch refusal in Branch 3
        and the genuinely ambiguous Branch 5 — because both end the match with
        an empty result. Money moved in Stripe but nothing changed in Revel, so
        the log line alone leaves the organizer believing a ticket was refunded.

        Runs inside the webhook's atomic block on purpose: the Notification rows
        commit with the rest of the handler (and vanish with it if the handler
        raises), while the notification dispatcher defers its Celery ``.delay()``
        to ``on_commit``. Redelivery can't double-notify because the
        ``StripeWebhookEvent`` dedup row in :func:`handle_event` stops the whole
        handler from re-running.

        Args:
            refund: The Stripe refund object dict that could not be matched.
            candidates: All Payment rows on the intent.
            payment_intent_id: The Stripe payment intent id.
        """
        unrefunded = [p for p in candidates if p.status != Payment.PaymentStatus.REFUNDED]
        if not unrefunded:
            return  # every Payment on the intent is already (fully) refunded — nothing to reconcile
        currency = unrefunded[0].currency
        send_refund_unmatched(
            payment_intent_id=payment_intent_id,
            refund_id=refund.get("id") or "",
            refund_amount=from_stripe_amount(int(refund.get("amount", 0)), currency),
            currency=currency,
            reason="non_uniform" if len(_distinct_amounts(candidates)) > 1 else "ambiguous",
            candidates=unrefunded,
        )

    def _schedule_credit_note(
        self,
        *,
        payment_intent_id: str,
        candidates: list[Payment],
        newly_refunded_ids: list[str],
        newly_refund_row_ids: list[str],
        touched_session_id: str | None,
    ) -> None:
        """Enqueue generate_attendee_credit_note_task after the refund loop.

        Handles two cases:
        - Normal path: one or more payments were just refunded → schedule with the new IDs.
        - Pure duplicate webhook: all candidates already succeeded → re-enqueue so that a
          previously failed .delay() (e.g. Redis hiccup) gets retried. Downstream is idempotent.

        Args:
            payment_intent_id: Stripe payment intent ID (used for logging only).
            candidates: All Payment rows for this intent.
            newly_refunded_ids: IDs of payments mutated in this invocation.
            newly_refund_row_ids: pks of the Refund rows applied in this invocation —
                threaded through so the credit note is amount-aware (#865).
            touched_session_id: stripe_session_id from the last mutated payment in this
                invocation, or None. All matched Payments share a charge/intent so any
                one is representative.
        """
        if newly_refunded_ids and touched_session_id:
            sid, ids, refund_ids = touched_session_id, newly_refunded_ids, newly_refund_row_ids

            def _trigger_credit_note() -> None:
                from events.tasks import generate_attendee_credit_note_task

                generate_attendee_credit_note_task.delay(sid, ids, refund_ids)

            transaction.on_commit(_trigger_credit_note)
            return

        # Gate on SUCCEEDED Refund rows, not on refund_status: a SUCCEEDED partial refund next to a PENDING
        # one leaves refund_status PENDING. The retry is idempotent downstream.
        succeeded_rows = list(
            Refund.objects.filter(payment__in=candidates, status=Refund.RefundStatus.SUCCEEDED).values_list(
                "pk", "payment_id"
            )
        )
        if not newly_refunded_ids and {pid for _, pid in succeeded_rows} >= {p.pk for p in candidates}:
            # Pure duplicate webhook — every candidate is already refunded.
            dup_sid = candidates[0].stripe_session_id
            dup_ids = [str(p.id) for p in candidates]
            dup_refund_ids = [str(pk) for pk, _ in succeeded_rows]

            def _retry_credit_note() -> None:
                from events.tasks import generate_attendee_credit_note_task

                generate_attendee_credit_note_task.delay(dup_sid, dup_ids, dup_refund_ids)

            transaction.on_commit(_retry_credit_note)
            logger.info(
                "stripe_webhook_duplicate_refund",
                payment_intent_id=payment_intent_id,
                payment_count=len(candidates),
            )

    @staticmethod
    def _match_by_known_refund_id(refund: dict[str, t.Any], candidates: list[Payment]) -> list[Payment]:
        """Branches 1 and 1.5: match via a refund id already known to us.

        Covers the legacy Payment mirror, any Refund row already carrying this
        Stripe refund id (a replay), and our own metadata pointer — the exact
        anchor every refund issued through Revel carries.

        Each DB leg below runs as a single bulk query across all candidates
        rather than one query per candidate: this method executes once per
        refund object while N Payment rows sit under ``select_for_update``,
        and a batch's size is unbounded, so a per-candidate query here would
        turn into O(N) round trips held under those locks.
        """
        refund_id: str | None = refund.get("id")

        # Legacy Payment mirror — already loaded, no query.
        for p in candidates:
            if refund_id and p.stripe_refund_id == refund_id:
                return [p]

        if refund_id:
            mirrored_ids = set(
                Refund.objects.filter(payment__in=candidates, stripe_refund_id=refund_id).values_list(
                    "payment_id", flat=True
                )
            )
            for p in candidates:
                if p.id in mirrored_ids:
                    return [p]

        metadata_refund_id = (refund.get("metadata") or {}).get("refund_id")
        if metadata_refund_id:
            pointed_ids = set(
                Refund.objects.filter(pk__in=_safe_uuid_list(metadata_refund_id), payment__in=candidates).values_list(
                    "payment_id", flat=True
                )
            )
            for p in candidates:
                if p.id in pointed_ids:
                    return [p]
        return []

    def _match_refund_to_payments(self, refund: dict[str, t.Any], candidates: list[Payment]) -> list[Payment]:
        """Return the Payment(s) this refund should apply to. Empty list = no match.

        Args:
            refund: A Stripe refund object dict from the charge's refunds list.
            candidates: All Payment rows sharing the same payment_intent_id.

        Returns:
            A list of matched Payment instances. Empty if the match is ambiguous or impossible.
        """
        refund_id: str | None = refund.get("id")
        refund_amount = int(refund.get("amount", 0))

        # Branches 1 and 1.5.
        matched = self._match_by_known_refund_id(refund, candidates)
        if matched:
            return matched

        # Branch 2: explicit metadata pointer.
        metadata_ticket_id: str | None = (refund.get("metadata") or {}).get("ticket_id")
        if metadata_ticket_id:
            for p in candidates:
                if str(p.ticket_id) == metadata_ticket_id:
                    return [p]

        # Branch 2.5: a single-Payment intent — any refund on it belongs to that payment,
        # whatever the amount (covers partial dashboard refunds). Guarded on status: a
        # fully-REFUNDED payment has no "remaining" amount to unambiguously absorb, so a
        # further refund on it falls through to Branch 5 (logged, no mutation) instead of
        # being silently matched here.
        if len(candidates) == 1 and candidates[0].status != Payment.PaymentStatus.REFUNDED:
            return candidates

        unrefunded = [p for p in candidates if p.status != Payment.PaymentStatus.REFUNDED]
        if not unrefunded:
            return []

        # Branch 3: exactly-one match on unrefunded rows' REMAINING refundable balance
        # — but only when every UNREFUNDED Payment on the intent has the same
        # remaining balance (an already-fully-REFUNDED payment has remaining 0 by
        # definition and carries no ambiguity of its own, so it's excluded from this
        # comparison — same as it's excluded from `exact` below). A prior partial
        # refund means gross amount is no longer what's owed, so the comparison must
        # be against remaining, not `p.amount`. On a batch with mixed remaining
        # balances, a partial refund on the payment with the larger balance is
        # indistinguishable from a full refund of the one with the smaller balance.
        # Dashboard refunds carry no ticket_id metadata, so there is nothing else to
        # disambiguate with: refuse and let Branch 5 log it.
        exact = [
            p
            for p in unrefunded
            if to_stripe_amount(refund_service.remaining_refundable(p), p.currency) == refund_amount
        ]
        if len(exact) == 1:
            remaining_amounts = _distinct_remaining_amounts(unrefunded)
            if len(remaining_amounts) == 1:
                return exact
            logger.warning(
                "stripe_refund_non_uniform_batch",
                payment_intent_id=candidates[0].stripe_payment_intent_id,
                refund_id=refund_id,
                refund_amount=refund_amount,
                would_have_matched_payment_id=str(exact[0].id),
                would_have_matched_ticket_id=str(exact[0].ticket_id),
                candidate_amounts=sorted(f"{amount}{currency}" for amount, currency in remaining_amounts),
            )

        # Branch 4: full-remaining-batch refund — sum each unrefunded payment's own
        # remaining refundable balance (not gross amount), so a batch containing an
        # already-partially-refunded payment is compared against what is actually
        # still owed.
        remaining_total = sum(to_stripe_amount(refund_service.remaining_refundable(p), p.currency) for p in unrefunded)
        if refund_amount == remaining_total:
            return unrefunded

        # Branch 5: ambiguous.
        return []

    def _sync_payment_refund_totals(
        self, payment: Payment, raw_response: dict[str, t.Any], *, refund_id: str, may_reopen: bool
    ) -> None:
        """Recompute the Payment's refund fields from its Refund rows. Record-only.

        - ``refund_amount`` is the sum of the SUCCEEDED rows (None when there is none).
        - ``refund_status`` is PENDING while a row is PENDING, else SUCCEEDED once money
          returned, else FAILED when a row failed.
        - The Payment becomes REFUNDED when the SUCCEEDED rows cover its amount. The
          ``save()`` sends the buyer the refunded email (``payment`` signal).
        - ``may_reopen``: a row failed in this delivery, so a REFUNDED Payment that the
          SUCCEEDED rows no longer cover goes back to SUCCEEDED. This uses a queryset
          ``update()`` on purpose: a ``save()`` here would fire the payment signal and
          email the buyer a payment confirmation for the status change.

        Args:
            payment: The locked Payment.
            raw_response: The full serialised webhook event (for audit).
            refund_id: The Stripe refund id that touched the Payment last.
            may_reopen: Whether a REFUNDED Payment may go back to SUCCEEDED.
        """
        rows = list(payment.refunds.all())
        succeeded = [r for r in rows if r.status == Refund.RefundStatus.SUCCEEDED]
        failed = [r for r in rows if r.status == Refund.RefundStatus.FAILED]
        total = sum((r.amount for r in succeeded), Decimal("0"))

        refund_status: Payment.RefundStatus | None
        if any(r.status == Refund.RefundStatus.PENDING for r in rows):
            refund_status = Payment.RefundStatus.PENDING
        elif total > 0:
            refund_status = Payment.RefundStatus.SUCCEEDED
        elif failed:
            refund_status = Payment.RefundStatus.FAILED
        else:
            refund_status = None

        fields: dict[str, t.Any] = {
            "stripe_refund_id": refund_id,
            "refund_amount": total if total > 0 else None,
            "refund_status": refund_status,
            "refund_failure_reason": failed[-1].failure_reason if refund_status == Payment.RefundStatus.FAILED else "",
            "refunded_at": max((r.succeeded_at or r.updated_at for r in succeeded), default=None),
            # The Payment is locked here: an erased buyer's payload is not stored (see stripe_payload_to_store).
            "raw_response": stripe_payload_to_store(payment, raw_response),
        }

        if total > 0 and total >= payment.amount:
            fields["status"] = Payment.PaymentStatus.REFUNDED
        elif may_reopen and payment.status == Payment.PaymentStatus.REFUNDED:
            Payment.objects.filter(pk=payment.pk).update(
                status=Payment.PaymentStatus.SUCCEEDED, updated_at=timezone.now(), **fields
            )
            payment.status = Payment.PaymentStatus.SUCCEEDED
            for name, value in fields.items():
                setattr(payment, name, value)
            return

        for name, value in fields.items():
            setattr(payment, name, value)
        payment.save(update_fields=[*fields, "updated_at"])
