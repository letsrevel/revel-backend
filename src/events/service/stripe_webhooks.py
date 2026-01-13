"""Stripe webhook event handlers."""

import stripe
import structlog
from django.db import transaction
from django.db.models import F

from events.models import Organization, Payment, Ticket, TicketTier

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

        # Check if already processed (idempotency)
        if all(p.status == Payment.PaymentStatus.SUCCEEDED for p in payments):
            logger.warning(
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
            # Store original status so signal handler can detect PENDING→ACTIVE transition
            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
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
        Automatically syncs the organization's Stripe connection status.
        """
        account_data = event.data.object
        account_id = account_data["id"]

        # Find the organization with this Stripe account
        try:
            organization = Organization.objects.get(stripe_account_id=account_id)
        except Organization.DoesNotExist:
            logger.warning("stripe_account_updated_unknown", account_id=account_id)
            return

        # Update the organization's Stripe status
        organization.stripe_charges_enabled = account_data.get("charges_enabled", False)
        organization.stripe_details_submitted = account_data.get("details_submitted", False)
        organization.save(update_fields=["stripe_charges_enabled", "stripe_details_submitted"])

        logger.info(
            "stripe_account_updated",
            organization_slug=organization.slug,
            account_id=account_id,
            charges_enabled=organization.stripe_charges_enabled,
            details_submitted=organization.stripe_details_submitted,
        )

    @transaction.atomic
    def handle_charge_refunded(self, event: stripe.Event) -> None:
        """Handle refund events from Stripe.

        When a connected account issues a refund (via Dashboard or API),
        this webhook updates the payment and ticket status.
        Stripe automatically refunds application fees proportionally.
        Supports both single-ticket and batch ticket purchases.
        """
        charge_data = event.data.object
        payment_intent_id = charge_data.get("payment_intent")

        if not payment_intent_id:
            logger.warning("stripe_refund_missing_intent", charge_id=charge_data.get("id"))
            return

        # Find all payments by payment_intent_id (supports batch purchases)
        payments = list(
            Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).select_related("ticket", "ticket__tier")
        )

        if not payments:
            logger.warning("stripe_refund_unknown_intent", payment_intent_id=payment_intent_id)
            return

        # Idempotency check
        if all(p.status == Payment.PaymentStatus.REFUNDED for p in payments):
            logger.warning(
                "stripe_webhook_duplicate_refund",
                payment_intent_id=payment_intent_id,
                payment_count=len(payments),
            )
            return

        raw_response = dict(event)
        refunded_tickets = []

        for payment in payments:
            if payment.status == Payment.PaymentStatus.REFUNDED:
                continue  # Already processed

            # Update payment status
            payment.status = Payment.PaymentStatus.REFUNDED
            payment.raw_response = raw_response
            payment.save(update_fields=["status", "raw_response"])

            # Cancel the ticket
            ticket = payment.ticket
            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
            ticket._refund_amount = f"{payment.amount} {payment.currency}"  # type: ignore[attr-defined]
            ticket.status = Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

            # Restore ticket quantity
            TicketTier.objects.filter(pk=ticket.tier.pk).update(quantity_sold=F("quantity_sold") - 1)
            refunded_tickets.append(ticket)

        # Notifications are now handled by Payment post_save signal in notifications/signals/payment.py
        logger.info(
            "stripe_refund_processed",
            payment_intent_id=payment_intent_id,
            payment_count=len(refunded_tickets),
            ticket_ids=[str(t.id) for t in refunded_tickets],
            total_amount=float(sum(p.amount for p in payments)),
            currency=payments[0].currency,
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

            # Restore ticket quantity
            TicketTier.objects.filter(pk=ticket.tier.pk).update(quantity_sold=F("quantity_sold") - 1)
            canceled_tickets.append(ticket)

        logger.info(
            "stripe_payment_intent_canceled_processed",
            payment_intent_id=payment_intent_id,
            payment_count=len(canceled_tickets),
            ticket_ids=[str(t.id) for t in canceled_tickets],
        )
