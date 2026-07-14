"""Stripe webhook event handlers."""

import functools
import typing as t
import uuid
from decimal import Decimal

import stripe
import structlog
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F

from accounts.models import RevelUser
from common.models import StripeConnectMixin
from events.exceptions import InvalidStripeWebhookSignatureError
from events.models import HeldSeriesPass, Organization, Payment, StripeWebhookEvent, Ticket, TicketTier
from events.service.waitlist_service import enqueue_waitlist_processing
from events.utils.currency import from_stripe_amount, to_stripe_amount
from notifications.signals.series_pass import send_series_pass_purchased

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service).
# This module makes its own outbound call (Refund.list in _resolve_refunds), so
# it must not rely on another module's import side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION

# Placeholder values that must never be treated as real signing secrets.
_PLACEHOLDER_SECRETS = frozenset({"whsec_...", "whsec_placeholder", ""})


def verify_webhook(payload: bytes, signature_header: str) -> stripe.Event:
    """Validate the ``Stripe-Signature`` header and return the parsed event.

    A two-endpoint Connect setup (platform "Your account" + "Connected
    accounts") points both endpoints at the same URL, each with its own
    ``whsec_*`` secret. We can't know up front which endpoint signed a given
    delivery, so we try each entry of ``settings.STRIPE_WEBHOOK_SECRETS`` in
    order; the first HMAC match wins. If none match — or no real secret is
    configured — we fail closed with ``InvalidStripeWebhookSignatureError``
    (rendered as 403 by the events exception handler).
    """
    secrets = [s for s in settings.STRIPE_WEBHOOK_SECRETS if s not in _PLACEHOLDER_SECRETS]
    if not secrets:
        logger.warning("stripe_webhook_secret_missing")
        raise InvalidStripeWebhookSignatureError()
    last_error: stripe.error.SignatureVerificationError | None = None
    for secret in secrets:
        try:
            return stripe.Webhook.construct_event(payload, signature_header, secret)
        except stripe.error.SignatureVerificationError as exc:
            last_error = exc
            continue
        except ValueError as exc:
            # construct_event parses JSON only AFTER the HMAC matched, so the
            # signature is fine and the body is malformed — retrying other
            # secrets can't change the verdict. Terminal.
            logger.warning("stripe_webhook_malformed_json", error=str(exc))
            raise InvalidStripeWebhookSignatureError() from exc
    logger.warning("stripe_webhook_signature_failed", error=str(last_error))
    raise InvalidStripeWebhookSignatureError()


@transaction.atomic
def handle_event(event: stripe.Event) -> None:
    """Route a verified event to its handler, idempotent on Stripe's event id.

    The ``StripeWebhookEvent`` INSERT is the idempotency token: Stripe
    redeliveries trip the unique constraint and skip the full handler. The
    insert runs in a nested atomic block (savepoint) so a duplicate-key
    violation doesn't poison the outer transaction. If the handler raises, the
    whole request transaction (ATOMIC_REQUESTS) rolls back including this row,
    so the next Stripe retry reprocesses the event.

    A duplicate is not a pure no-op: the first delivery's ``on_commit``
    ``.delay()`` calls ran *after* its commit and may have failed (e.g. broker
    outage) without rolling anything back, so redelivery is Stripe's retry
    mechanism for exactly that window — :meth:`StripeEventHandler.replay`
    re-enqueues the idempotent downstream tasks.
    """
    try:
        with transaction.atomic():
            record = StripeWebhookEvent.objects.create(
                event_id=event.id,
                event_type=event.type,
                account=getattr(event, "account", "") or "",
                livemode=bool(getattr(event, "livemode", False)),
                payload=dict(event),
            )
    except IntegrityError, DjangoValidationError:
        # IntegrityError = DB unique violation (race past full_clean);
        # ValidationError = TimeStampedModel.save's full_clean caught the
        # duplicate first. Either way: redelivery — retry the post-commit
        # task dispatches, then stop.
        logger.info("stripe_webhook_duplicate", event_id=event.id, event_type=event.type)
        StripeEventHandler(event).replay()
        return

    handled = StripeEventHandler(event).handle()
    record.outcome = StripeWebhookEvent.Outcome.HANDLED if handled else StripeWebhookEvent.Outcome.UNHANDLED
    record.save(update_fields=["outcome", "updated_at"])


class StripeEventHandler:
    """Handles the business logic for different types of Stripe webhook events."""

    def __init__(self, event: stripe.Event):
        """Initialize the Stripe event handler."""
        self.event = event

    def handle(self) -> bool:
        """Route the event to its handler. Returns True if the type is mapped."""
        handlers: dict[str, t.Callable[[stripe.Event], None]] = {
            "checkout.session.completed": self.handle_checkout_session_completed,
            "account.updated": self.handle_account_updated,
            "charge.refunded": self.handle_charge_refunded,
            "payment_intent.canceled": self.handle_payment_intent_canceled,
        }
        handler = handlers.get(self.event.type)
        if handler is None:
            self.handle_unknown_event(self.event)
            return False
        handler(self.event)
        return True

    def handle_unknown_event(self, event: stripe.Event) -> None:
        """Log unhandled event types for future development."""
        logger.info("stripe_webhook_unhandled_event", event_type=event.type, event_id=event.id)

    def replay(self) -> None:
        """Re-enqueue idempotent downstream tasks for a redelivered event.

        The dedup gate in :func:`handle_event` stops the full handler from
        re-running, but the first delivery's ``transaction.on_commit``
        ``.delay()`` calls fired after that commit and may have failed (e.g.
        broker outage) without rolling the dedup row back. Stripe redelivery
        is the retry mechanism for that window, so re-enqueue the tasks here;
        downstream is idempotent and skips work already done.
        """
        replayers: dict[str, t.Callable[[stripe.Event], None]] = {
            "checkout.session.completed": self.replay_checkout_session_completed,
            "charge.refunded": self.replay_charge_refunded,
        }
        replayer = replayers.get(self.event.type)
        if replayer is not None:
            replayer(self.event)

    def replay_checkout_session_completed(self, event: stripe.Event) -> None:
        """Re-enqueue invoice generation for a redelivered checkout completion.

        Mirrors the unconditional enqueue (and its guards) in
        :meth:`handle_checkout_session_completed` so a ``.delay()`` that
        failed on the first delivery gets retried.
        """
        session = event.data.object
        session_id = session["id"]
        if session["payment_status"] not in {"paid", "no_payment_required"}:
            return
        if not Payment.objects.filter(stripe_session_id=session_id).exists():
            return

        def _trigger_invoice() -> None:
            from events.tasks import generate_attendee_invoice_task

            generate_attendee_invoice_task.delay(session_id)

        transaction.on_commit(_trigger_invoice)

    def replay_charge_refunded(self, event: stripe.Event) -> None:
        """Re-enqueue credit-note generation for a redelivered refund.

        Mirrors the "pure duplicate webhook" branch of
        :meth:`_schedule_credit_note`: the payments the first delivery
        refunded are ``refund_status=SUCCEEDED`` by now, so retry the credit
        note for exactly those.
        """
        payment_intent_id = event.data.object.get("payment_intent")
        if not payment_intent_id:
            return
        refunded = list(
            Payment.objects.filter(
                stripe_payment_intent_id=payment_intent_id,
                refund_status=Payment.RefundStatus.SUCCEEDED,
            )
        )
        if not refunded:
            return
        sid = refunded[0].stripe_session_id
        ids = [str(p.id) for p in refunded]

        def _retry_credit_note() -> None:
            from events.tasks import generate_attendee_credit_note_task

            generate_attendee_credit_note_task.delay(sid, ids)

        transaction.on_commit(_retry_credit_note)

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
            if ticket.status == Ticket.TicketStatus.CANCELLED:
                # Defense-in-depth: a late payment on a checkout whose ticket was
                # cancelled in the meantime (e.g. organizer cancelled a pending
                # series pass) must not resurrect the ticket. Payment bookkeeping
                # above still runs — the money was captured and needs a refund.
                logger.warning(
                    "stripe_session_completed_cancelled_ticket_skipped",
                    session_id=session_id,
                    ticket_id=str(ticket.id),
                )
                continue
            ticket.status = Ticket.TicketStatus.ACTIVE
            ticket.save(update_fields=["status"])

        self._activate_series_passes(session_id)

        # Notifications are now handled by Payment post_save signal in notifications/signals/payment.py
        logger.info(
            "stripe_payment_success",
            session_id=session_id,
            payment_count=len(payments),
            ticket_ids=[str(p.ticket_id) for p in payments],
            total_amount=float(sum(p.amount for p in payments)),
            currency=payments[0].currency,
        )

    @staticmethod
    def _activate_series_passes(session_id: str) -> None:
        """Activate any series pass bought in this session (idempotent).

        ``.update()`` intentionally skips signals — ids are captured beforehand so
        the purchase notification can be sent explicitly (once, on commit). Each
        activated pass is then backfilled: the extension task only materializes
        tickets for ACTIVE holders, so without this a mid-checkout buyer would
        miss any extension linked while the pass sat PENDING.
        """
        # Imported here to avoid a cycle (series_pass_service -> events.tasks -> services).
        from events.service.series_pass_service import backfill_missing_tickets

        activated_pass_ids = list(
            HeldSeriesPass.objects.filter(
                stripe_session_id=session_id, status=HeldSeriesPass.HeldSeriesPassStatus.PENDING
            ).values_list("id", flat=True)
        )
        if not activated_pass_ids:
            return
        HeldSeriesPass.objects.filter(id__in=activated_pass_ids).update(
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE
        )
        for held_pass in HeldSeriesPass.objects.filter(id__in=activated_pass_ids).select_related("series_pass", "user"):
            backfill_missing_tickets(held_pass)
        for held_pass_id in activated_pass_ids:
            transaction.on_commit(functools.partial(send_series_pass_purchased, held_pass_id))

    @transaction.atomic
    def handle_account_updated(self, event: stripe.Event) -> None:
        """Handle updates to connected Stripe accounts.

        This webhook fires when account details change, including when
        charges_enabled and details_submitted status change during onboarding.
        Syncs the status on whichever model owns the account (Organization or RevelUser).
        """
        if not getattr(event, "account", None):
            # account.updated for the platform's OWN account (only possible if
            # the platform endpoint ever subscribes to it) — nothing to mirror;
            # host-org binding is managed via the admin action.
            # warning, not debug: reaching this branch means the platform
            # endpoint is subscribed to account.updated, which the provisioning
            # command never does — surface the misconfiguration in prod logs.
            logger.warning("stripe_account_updated_platform_self_skipped", event_id=event.id)
            return

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

        newly_refunded_ids, touched_session_id, affected_event_ids = self._process_refunds(
            refunds=refunds,
            candidates=candidates,
            raw_response=dict(event),
            payment_intent_id=payment_intent_id,
        )

        # One enqueue per event regardless of how many tickets cancelled inside it;
        # the processor scans all freed seats in a single pass.
        for event_id in affected_event_ids:
            enqueue_waitlist_processing(event_id)

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
    ) -> tuple[list[str], str | None, set[uuid.UUID]]:
        """Match each refund to its Payment(s) and apply it.

        Args:
            refunds: The charge's refund object dicts (from _resolve_refunds).
            candidates: All locked Payment rows for this intent.
            raw_response: The full serialised webhook event (for audit).
            payment_intent_id: Stripe payment intent id (logging only).

        Returns:
            A ``(newly_refunded_ids, touched_session_id, affected_event_ids)``
            tuple: the Payment ids mutated in this call, the session id of the
            last mutated Payment (or None), and the event ids whose capacity
            was freed by a ticket cancellation.
        """
        touched_session_id: str | None = None
        newly_refunded_ids: list[str] = []
        affected_event_ids: set[uuid.UUID] = set()

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
                cancelled_event_id = self._apply_refund_to_payment(payment, refund, raw_response, allocated_amount)
                newly_refunded_ids.append(str(payment.id))
                touched_session_id = payment.stripe_session_id
                if cancelled_event_id is not None:
                    affected_event_ids.add(cancelled_event_id)

        return newly_refunded_ids, touched_session_id, affected_event_ids

    def _resolve_refunds(self, charge_data: dict[str, t.Any]) -> list[dict[str, t.Any]]:
        """Return the charge's refunds, from the payload or the Stripe API.

        Payloads rendered at API versions >= 2022-11-15 don't embed the
        charge's refunds list, so any *pinned* webhook endpoint delivers
        ``charge.refunded`` without it — fetch it outbound in that case.
        Embedded refunds (old unpinned endpoints) short-circuit without an
        API call.

        Connected-account events carry ``event.account``; the refunds then
        live on that account, so the request needs the ``Stripe-Account``
        header. A failed call raises, rolling the webhook transaction (and the
        dedup row) back so the Stripe retry reprocesses the event.

        Args:
            charge_data: The Charge object from the webhook payload.

        Returns:
            The charge's refunds as dicts. Empty if the charge has none (or
            the payload carries no charge id).
        """
        embedded = charge_data.get("refunds", {}).get("data", []) or []
        if embedded:
            return list(embedded)
        charge_id = charge_data.get("id")
        if not charge_id:
            return []
        params: dict[str, t.Any] = {"charge": charge_id, "limit": 100}
        if account := getattr(self.event, "account", None):
            params["stripe_account"] = account
        # auto_paging_iter walks past the first page (limit is just page size),
        # so a charge with >100 refunds doesn't silently drop the tail.
        return [dict(refund) for refund in stripe.Refund.list(**params).auto_paging_iter()]

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
    ) -> uuid.UUID | None:
        """Persist refund data onto a Payment and cancel its linked Ticket.

        Args:
            payment: The Payment instance to update.
            refund: The Stripe refund object dict.
            raw_response: The full serialised webhook event (for audit).
            allocated_amount: The refund amount attributable to THIS Payment in
                major currency units. For single-Payment matches this equals the
                Stripe refund amount converted from smallest units; for a
                full-batch sweep (Branch 4) this equals ``payment.amount``.

        Returns:
            The event_id of the cancelled ticket if the ticket transitioned to
            CANCELLED in this call (capacity now freed), otherwise None. The
            caller batches these into a single waitlist enqueue per event.
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
            return ticket.event_id
        return None

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

        # Find all payments by payment_intent_id (supports batch purchases). Locked
        # with select_for_update(of=("self",)) — mirroring handle_charge_refunded —
        # so a concurrent reclaim on the same rows (cleanup_expired_payments,
        # cancel_pending_checkout) serializes instead of both reading PENDING and
        # both decrementing the tier from the same unlocked snapshot. No outbound
        # Stripe call happens in this handler, so holding the lock for its duration
        # is safe (#632).
        payments = list(
            Payment.objects.select_for_update(of=("self",))
            .filter(stripe_payment_intent_id=payment_intent_id)
            .select_related("ticket", "ticket__tier")
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
        affected_event_ids: set[uuid.UUID] = set()

        # Cancel any series pass stranded by the dead checkout FIRST, releasing its
        # quantity_sold so the buyer can purchase again — pass row before tier rows,
        # matching SeriesPassPurchaseService.purchase's lock order to avoid
        # deadlocking against a concurrent purchase on the same pass. Imported here
        # to avoid a cycle (series_pass_service -> events.tasks -> services).
        from events.service.series_pass_service import expire_stranded_held_passes

        expire_stranded_held_passes({p.stripe_session_id for p in pending_payments})

        for payment in pending_payments:
            # Update payment status to failed
            payment.status = Payment.PaymentStatus.FAILED
            payment.raw_response = raw_response
            payment.save(update_fields=["status", "raw_response"])

            # Cancel the ticket
            ticket = payment.ticket
            already_cancelled = ticket.status == Ticket.TicketStatus.CANCELLED
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

            # Restore ticket quantity (guard against underflow if row is already at 0).
            # Skip if already CANCELLED: a PENDING payment can be paired with an
            # already-CANCELLED ticket (e.g. cancel_ticket_by_user already released
            # the slot) whose capacity must not be released a second time. Safe by
            # invariant today -- a PENDING payment has no payment_intent_id, so this
            # handler can't match it -- but mirrors handle_charge_refunded's guard for
            # consistency and defense-in-depth (#632).
            if not already_cancelled:
                TicketTier.objects.filter(pk=ticket.tier.pk, quantity_sold__gt=0).update(
                    quantity_sold=F("quantity_sold") - 1
                )
            affected_event_ids.add(ticket.event_id)
            canceled_tickets.append(ticket)

        # One enqueue per event regardless of how many tickets cancelled inside it.
        for event_id in affected_event_ids:
            enqueue_waitlist_processing(event_id)

        logger.info(
            "stripe_payment_intent_canceled_processed",
            payment_intent_id=payment_intent_id,
            payment_count=len(canceled_tickets),
            ticket_ids=[str(t.id) for t in canceled_tickets],
        )
