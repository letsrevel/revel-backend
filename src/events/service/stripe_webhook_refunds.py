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
from decimal import Decimal

import stripe
import structlog
from django.db import transaction
from django.db.models import Sum

from events.models import Payment, Refund
from events.utils.currency import from_stripe_amount, to_stripe_amount
from notifications.signals.payment import send_refund_unmatched

logger = structlog.get_logger(__name__)


def _distinct_amounts(payments: list[Payment]) -> set[tuple[int, str]]:
    """Return the distinct (smallest-unit amount, currency) pairs across payments."""
    return {(to_stripe_amount(p.amount, p.currency), p.currency) for p in payments}


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
        def _resolve_refunds(self, charge_data: dict[str, t.Any]) -> list[dict[str, t.Any]]: ...

    def _handle_ticket_refunds(self, event: stripe.Event, payment_intent_id: str) -> None:
        """Match each refund object in the charge to its specific ticket Payment row.

        Record-only: applying a match never cancels a ticket or reclaims tier
        capacity (see :meth:`_apply_refund_to_payment`) — cancel and refund are
        orthogonal operations.

        Matching strategy (first match wins):
          1. existing stripe_refund_id on a Payment or Refund row
          1.5. refund.metadata["refund_id"] — our own Refund row pointer
          2. refund.metadata["ticket_id"]
          2.5. exactly one Payment on the intent — unambiguous by construction
          3. exactly one unrefunded Payment with matching amount, *and* the intent's
             Payments are uniform in amount (otherwise the match is a guess)
          4. refund.amount equals sum of unrefunded-payment amounts (full remaining batch)
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
            .select_related("ticket", "ticket__tier")
        )
        if not candidates:
            logger.warning("stripe_refund_unknown_intent", payment_intent_id=payment_intent_id)
            return

        newly_refunded_ids, touched_session_id = self._process_refunds(
            refunds=refunds,
            candidates=candidates,
            raw_response=dict(event),
            payment_intent_id=payment_intent_id,
        )

        self._schedule_credit_note(
            payment_intent_id=payment_intent_id,
            candidates=candidates,
            newly_refunded_ids=newly_refunded_ids,
            touched_session_id=touched_session_id,
        )

        logger.info(
            "stripe_refund_processed",
            payment_intent_id=payment_intent_id,
            refund_count=len(refunds),
            newly_refunded_payment_ids=newly_refunded_ids,
        )

    def _process_refunds(
        self,
        *,
        refunds: list[dict[str, t.Any]],
        candidates: list[Payment],
        raw_response: dict[str, t.Any],
        payment_intent_id: str,
    ) -> tuple[list[str], str | None]:
        """Match each refund to its Payment(s) and apply it.

        Record-only — see :meth:`_apply_refund_to_payment`.

        Args:
            refunds: The charge's refund object dicts (from _resolve_refunds).
            candidates: All locked Payment rows for this intent.
            raw_response: The full serialised webhook event (for audit).
            payment_intent_id: Stripe payment intent id (logging only).

        Returns:
            A ``(newly_refunded_ids, touched_session_id)`` tuple: the Payment
            ids mutated in this call, and the session id of the last mutated
            Payment (or None).
        """
        touched_session_id: str | None = None
        newly_refunded_ids: list[str] = []

        for refund in refunds:
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
                self._notify_unmatched_refund(refund, candidates, payment_intent_id)
                continue
            # Branch 4 fans out a single refund across N Payments — each gets its
            # own amount, not the aggregate. Branches 1-3 always return one row.
            is_full_batch = len(matched) > 1
            for payment in matched:
                if Refund.objects.filter(
                    payment=payment, stripe_refund_id=refund["id"], status=Refund.RefundStatus.SUCCEEDED
                ).exists():
                    continue  # idempotent replay of this specific refund
                allocated_amount = (
                    payment.amount
                    if is_full_batch
                    else from_stripe_amount(int(refund.get("amount", 0)), payment.currency)
                )
                self._apply_refund_to_payment(payment, refund, raw_response, allocated_amount)
                newly_refunded_ids.append(str(payment.id))
                touched_session_id = payment.stripe_session_id

        return newly_refunded_ids, touched_session_id

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
            touched_session_id: stripe_session_id from the last mutated payment in this
                invocation, or None. All matched Payments share a charge/intent so any
                one is representative.
        """
        if newly_refunded_ids and touched_session_id:
            sid, ids = touched_session_id, newly_refunded_ids

            def _trigger_credit_note() -> None:
                from events.tasks import generate_attendee_credit_note_task

                generate_attendee_credit_note_task.delay(sid, ids)

            transaction.on_commit(_trigger_credit_note)
            return

        if not newly_refunded_ids and all(p.refund_status == Payment.RefundStatus.SUCCEEDED for p in candidates):
            # Pure duplicate webhook — every candidate is already refunded.
            dup_sid = candidates[0].stripe_session_id
            dup_ids = [str(p.id) for p in candidates]

            def _retry_credit_note() -> None:
                from events.tasks import generate_attendee_credit_note_task

                generate_attendee_credit_note_task.delay(dup_sid, dup_ids)

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
        """
        refund_id: str | None = refund.get("id")
        for p in candidates:
            if p.stripe_refund_id and p.stripe_refund_id == refund_id:
                return [p]
            if refund_id and Refund.objects.filter(payment=p, stripe_refund_id=refund_id).exists():
                return [p]

        metadata_refund_id = (refund.get("metadata") or {}).get("refund_id")
        if metadata_refund_id:
            for p in candidates:
                if Refund.objects.filter(pk__in=_safe_uuid_list(metadata_refund_id), payment=p).exists():
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
        # whatever the amount (covers partial dashboard refunds).
        if len(candidates) == 1:
            return candidates

        unrefunded = [p for p in candidates if p.status != Payment.PaymentStatus.REFUNDED]
        if not unrefunded:
            return []

        # Branch 3: exactly-one exact-amount match among unrefunded rows — but only
        # when every Payment on the intent costs the same. On a mixed-price batch a
        # partial refund on the expensive ticket is indistinguishable from a full
        # refund of the cheap one, and guessing wrong cancels a ticket whose buyer
        # still occupies the seat. Dashboard refunds carry no ticket_id metadata, so
        # there is nothing else to disambiguate with: refuse and let Branch 5 log it.
        exact = [p for p in unrefunded if to_stripe_amount(p.amount, p.currency) == refund_amount]
        if len(exact) == 1:
            amounts = _distinct_amounts(candidates)
            if len(amounts) == 1:
                return exact
            logger.warning(
                "stripe_refund_non_uniform_batch",
                payment_intent_id=candidates[0].stripe_payment_intent_id,
                refund_id=refund_id,
                refund_amount=refund_amount,
                would_have_matched_payment_id=str(exact[0].id),
                would_have_matched_ticket_id=str(exact[0].ticket_id),
                candidate_amounts=sorted(f"{amount}{currency}" for amount, currency in amounts),
            )

        # Branch 4: full-remaining-batch refund.
        remaining_total = sum(to_stripe_amount(p.amount, p.currency) for p in unrefunded)
        if refund_amount == remaining_total:
            return unrefunded

        # Branch 5: ambiguous.
        return []

    def _apply_refund_to_payment(
        self,
        payment: Payment,
        refund: dict[str, t.Any],
        raw_response: dict[str, t.Any],
        allocated_amount: Decimal,
    ) -> None:
        """Persist refund data onto its Refund row and the Payment. Record-only.

        Refunds no longer cancel tickets: cancel and refund are orthogonal
        operations, and a Stripe-Dashboard refund leaves the ticket valid until
        the organizer explicitly cancels it.

        Args:
            payment: The Payment instance to update.
            refund: The Stripe refund object dict.
            raw_response: The full serialised webhook event (for audit).
            allocated_amount: The refund amount attributable to THIS Payment in
                major currency units. For single-Payment matches this equals the
                Stripe refund amount converted from smallest units; for a
                full-batch sweep (Branch 4) this equals ``payment.amount``. Only
                used when no existing Refund row is found for this refund —
                a pre-existing (e.g. PENDING, app-initiated) row keeps its own
                requested amount.
        """
        from django.utils import timezone

        refund_id: str = refund["id"]
        metadata_refund_id = (refund.get("metadata") or {}).get("refund_id")

        row: Refund | None = None
        if metadata_refund_id:
            row = Refund.objects.filter(pk__in=_safe_uuid_list(metadata_refund_id), payment=payment).first()
        if row is None:
            row = Refund.objects.filter(payment=payment, stripe_refund_id=refund_id).first()
        if row is None:
            row = Refund(
                payment=payment,
                amount=allocated_amount,
                currency=payment.currency,
                source=Refund.Source.STRIPE_DASHBOARD,
            )
        row.stripe_refund_id = refund_id
        row.status = Refund.RefundStatus.SUCCEEDED
        row.failure_reason = ""
        row.save()

        total = payment.refunds.filter(status=Refund.RefundStatus.SUCCEEDED).aggregate(total=Sum("amount"))[
            "total"
        ] or Decimal("0")
        payment.stripe_refund_id = refund_id
        payment.refund_amount = total
        payment.refund_status = Payment.RefundStatus.SUCCEEDED
        payment.refunded_at = timezone.now()
        if total >= payment.amount:
            payment.status = Payment.PaymentStatus.REFUNDED
        payment.raw_response = raw_response
        payment.save(
            update_fields=[
                "stripe_refund_id",
                "refund_amount",
                "refund_status",
                "refunded_at",
                "status",
                "raw_response",
            ]
        )
