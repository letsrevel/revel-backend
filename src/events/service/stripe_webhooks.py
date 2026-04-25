"""Stripe webhook event handlers."""

import typing as t
from decimal import Decimal

import stripe
import structlog
from django.db import transaction
from django.db.models import F

from accounts.models import RevelUser
from common.models import StripeConnectMixin
from events.models import Organization, Payment, Ticket, TicketTier
from events.utils.currency import from_stripe_amount, to_stripe_amount

logger = structlog.get_logger(__name__)


class StripeEventHandler:
    """Handles the business logic for different types of Stripe webhook events."""

    def __init__(self, event: stripe.Event):
        """Initialize the Stripe event handler."""
        self.event = event

    def handle(self) -> None:
        """Routes the event to the appropriate handler based on its type."""
        event_type = self.event.type
        handler_method = getattr(self, f"handle_{event_type.replace('.', '_')}", self.handle_unknown_event)
        handler_method(self.event)

    def handle_unknown_event(self, event: stripe.Event) -> None:
        """Log unhandled event types for future development."""
        logger.info("stripe_webhook_unhandled_event", event_type=event.type, event_id=event.id)

    @transaction.atomic
    def handle_checkout_session_completed(self, event: stripe.Event) -> None:
        """Handles the successful completion of a checkout session.

        Updates payment and ticket status and triggers confirmation email.
        Supports both single-ticket and batch ticket purchases.
        """
        session = event.data.object
        session_id = session["id"]

        if session["payment_status"] not in {"paid", "no_payment_required"}:
            logger.warning(
                "stripe_session_unresolved_payment",
                session_id=session_id,
                payment_status=session["payment_status"],
            )
            return

        # Get all payments for this session (supports batch purchases)
        payments = list(Payment.objects.filter(stripe_session_id=session_id).select_related("ticket"))

        if not payments:
            logger.warning("stripe_session_no_payments", session_id=session_id)
            return

        # Always enqueue invoice generation (idempotent downstream).
        # This must run even on duplicate webhooks so that a previously failed
        # .delay() call gets retried when Stripe re-delivers the event.
        def _trigger_invoice() -> None:
            from events.tasks import generate_attendee_invoice_task

            generate_attendee_invoice_task.delay(session_id)

        transaction.on_commit(_trigger_invoice)

        # Check if already processed (idempotency for payment/ticket updates)
        if all(p.status == Payment.PaymentStatus.SUCCEEDED for p in payments):
            logger.info(
                "stripe_webhook_duplicate_payment_success",
                session_id=session_id,
                payment_count=len(payments),
            )
            return

        payment_intent_id = session.get("payment_intent")
        raw_response = dict(event)

        # Update all payments and tickets
        for payment in payments:
            if payment.status == Payment.PaymentStatus.SUCCEEDED:
                continue  # Already processed

            payment.status = Payment.PaymentStatus.SUCCEEDED
            payment.stripe_payment_intent_id = payment_intent_id
            payment.raw_response = raw_response
            payment.save(update_fields=["status", "stripe_payment_intent_id", "raw_response"])

            ticket = payment.ticket
            ticket.status = Ticket.TicketStatus.ACTIVE
            ticket.save(update_fields=["status"])

        # Notifications are now handled by Payment post_save signal in notifications/signals/payment.py
        logger.info(
            "stripe_payment_success",
            session_id=session_id,
            payment_count=len(payments),
            ticket_ids=[str(p.ticket_id) for p in payments],
            total_amount=float(sum(p.amount for p in payments)),
            currency=payments[0].currency,
        )

    @transaction.atomic
    def handle_account_updated(self, event: stripe.Event) -> None:
        """Handle updates to connected Stripe accounts.

        This webhook fires when account details change, including when
        charges_enabled and details_submitted status change during onboarding.
        Syncs the status on whichever model owns the account (Organization or RevelUser).
        """
        account_data = event.data.object
        account_id = account_data["id"]

        connectable = self._find_connectable(account_id)
        if connectable is None:
            logger.warning("stripe_account_updated_unknown", account_id=account_id)
            return

        connectable.stripe_charges_enabled = account_data.get("charges_enabled", False)
        connectable.stripe_details_submitted = account_data.get("details_submitted", False)
        connectable.save(
            update_fields=connectable._stripe_update_fields("stripe_charges_enabled", "stripe_details_submitted"),
        )

        if isinstance(connectable, Organization):
            logger.info(
                "stripe_account_updated",
                organization_slug=connectable.slug,
                account_id=account_id,
                charges_enabled=connectable.stripe_charges_enabled,
                details_submitted=connectable.stripe_details_submitted,
            )
        else:
            logger.info(
                "stripe_account_updated",
                user_id=str(connectable.pk),
                account_id=account_id,
                charges_enabled=connectable.stripe_charges_enabled,
                details_submitted=connectable.stripe_details_submitted,
            )

    @staticmethod
    def _find_connectable(account_id: str) -> StripeConnectMixin | None:
        """Look up the model that owns *account_id* (Organization first, then RevelUser)."""
        try:
            return Organization.objects.get(stripe_account_id=account_id)
        except Organization.DoesNotExist:
            pass
        try:
            return RevelUser.objects.get(stripe_account_id=account_id)
        except RevelUser.DoesNotExist:
            return None

    @transaction.atomic
    def handle_charge_refunded(self, event: stripe.Event) -> None:
        """Match each refund object in the charge to its specific Payment row.

        Five-branch matching strategy (first match wins):
          1. existing stripe_refund_id on a Payment
          2. refund.metadata["ticket_id"]
          3. exactly one unrefunded Payment with matching amount
          4. refund.amount equals sum of unrefunded-payment amounts (full remaining batch)
          5. ambiguous → logged, no mutation
        """
        charge_data = event.data.object
        payment_intent_id = charge_data.get("payment_intent")

        if not payment_intent_id:
            logger.warning("stripe_refund_missing_intent", charge_id=charge_data.get("id"))
            return

        # Lock Payment rows for the duration of this transaction. Stripe webhooks
        # are at-least-once, and a Stripe-Dashboard refund's webhook can also race
        # against an in-flight user-initiated cancel (which itself locks the same
        # Payment via cancellation_service). Without the lock here, two concurrent
        # transactions both observe `refund_status is None`, both pass the SUCCEEDED
        # skip below, and both decrement `tier.quantity_sold`. Locking with
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

        refunds = charge_data.get("refunds", {}).get("data", []) or []
        if not refunds:
            logger.warning("stripe_refund_event_no_refund_data", payment_intent_id=payment_intent_id)
            return

        raw_response = dict(event)
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
                    candidate_payment_ids=[str(c.id) for c in candidates if c.refund_status is None],
                )
                continue
            # Branch 4 fans out a single refund across N Payments — each gets its
            # own amount, not the aggregate. Branches 1-3 always return one row.
            is_full_batch = len(matched) > 1
            for payment in matched:
                if payment.refund_status == Payment.RefundStatus.SUCCEEDED:
                    continue  # idempotent replay
                allocated_amount = (
                    payment.amount
                    if is_full_batch
                    else from_stripe_amount(int(refund.get("amount", 0)), payment.currency)
                )
                self._apply_refund_to_payment(payment, refund, raw_response, allocated_amount)
                newly_refunded_ids.append(str(payment.id))
                touched_session_id = payment.stripe_session_id

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

        # Branch 1: already-known refund id.
        for p in candidates:
            if p.stripe_refund_id and p.stripe_refund_id == refund_id:
                return [p]

        # Branch 2: explicit metadata pointer.
        metadata_ticket_id: str | None = (refund.get("metadata") or {}).get("ticket_id")
        if metadata_ticket_id:
            for p in candidates:
                if str(p.ticket_id) == metadata_ticket_id:
                    return [p]

        unrefunded = [p for p in candidates if p.refund_status is None]
        if not unrefunded:
            return []

        # Branch 3: exactly-one exact-amount match among unrefunded rows.
        exact = [p for p in unrefunded if to_stripe_amount(p.amount, p.currency) == refund_amount]
        if len(exact) == 1:
            return exact

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
        """Persist refund data onto a Payment and cancel its linked Ticket.

        Args:
            payment: The Payment instance to update.
            refund: The Stripe refund object dict.
            raw_response: The full serialised webhook event (for audit).
            allocated_amount: The refund amount attributable to THIS Payment in
                major currency units. For single-Payment matches this equals the
                Stripe refund amount converted from smallest units; for a
                full-batch sweep (Branch 4) this equals ``payment.amount``.
        """
        from django.utils import timezone

        from events.models.ticket import CancellationSource

        payment.stripe_refund_id = refund["id"]
        payment.refund_amount = allocated_amount
        payment.refund_status = Payment.RefundStatus.SUCCEEDED
        payment.refunded_at = timezone.now()
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

        ticket = payment.ticket
        if ticket.status != Ticket.TicketStatus.CANCELLED:
            ticket._refund_amount = str(payment.refund_amount)  # type: ignore[attr-defined]
            ticket._refund_currency = payment.currency  # type: ignore[attr-defined]
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.cancelled_at = timezone.now()
            ticket.cancellation_source = CancellationSource.STRIPE_DASHBOARD
            ticket.save(update_fields=["status", "cancelled_at", "cancellation_source"])
            TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )

    @transaction.atomic
    def handle_payment_intent_canceled(self, event: stripe.Event) -> None:
        """Handle canceled payment intents.

        This fires when a payment is canceled before being captured.
        For example, when a checkout session expires without payment.
        Supports both single-ticket and batch ticket purchases.
        """
        payment_intent_data = event.data.object
        payment_intent_id = payment_intent_data.get("id")

        if not payment_intent_id:
            logger.warning("stripe_payment_intent_canceled_missing_id")
            return

        # Find all payments by payment_intent_id (supports batch purchases)
        payments = list(
            Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).select_related("ticket", "ticket__tier")
        )

        if not payments:
            # This is expected for sessions that expire naturally before payment
            logger.debug("stripe_payment_intent_canceled_unknown", payment_intent_id=payment_intent_id)
            return

        # Only process pending payments
        pending_payments = [p for p in payments if p.status == Payment.PaymentStatus.PENDING]
        if not pending_payments:
            logger.info(
                "stripe_payment_intent_canceled_no_pending",
                payment_intent_id=payment_intent_id,
                payment_count=len(payments),
            )
            return

        raw_response = dict(event)
        canceled_tickets = []

        for payment in pending_payments:
            # Update payment status to failed
            payment.status = Payment.PaymentStatus.FAILED
            payment.raw_response = raw_response
            payment.save(update_fields=["status", "raw_response"])

            # Cancel the ticket
            ticket = payment.ticket
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

            # Restore ticket quantity (guard against underflow if row is already at 0).
            TicketTier.objects.filter(pk=ticket.tier.pk, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            canceled_tickets.append(ticket)

        logger.info(
            "stripe_payment_intent_canceled_processed",
            payment_intent_id=payment_intent_id,
            payment_count=len(canceled_tickets),
            ticket_ids=[str(t.id) for t in canceled_tickets],
        )
