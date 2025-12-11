import typing as t
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import stripe
import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from pydantic import EmailStr
from stripe.checkout import Session

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization, Payment, Ticket, TicketTier

logger = structlog.get_logger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def create_connect_account(organization: Organization, stripe_account_email: EmailStr) -> str:
    """Create a Stripe Connect Standard account for an organization."""
    account = stripe.Account.create(type="standard", email=stripe_account_email)
    organization.stripe_account_id = account.id
    organization.stripe_account_email = stripe_account_email
    organization.save(update_fields=["stripe_account_id", "stripe_account_email"])
    return t.cast(str, account.id)


def create_account_link(account_id: str, organization: Organization) -> str:
    """Create a one-time onboarding link for a Stripe Connect account."""
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    refresh_url = f"{frontend_base_url}/org/{organization.slug}/admin/settings?stripe_refresh=true"
    return_url = f"{frontend_base_url}/org/{organization.slug}/admin/settings?stripe_success=true"
    account_link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return t.cast(str, account_link.url)


def get_account_details(account_id: str) -> stripe.Account:
    """Retrieve details for a connected Stripe account."""
    return t.cast(stripe.Account, stripe.Account.retrieve(account_id))


def stripe_verify_account(organization: Organization) -> Organization:
    """Verify a Stripe Connect account."""
    if organization.stripe_account_id is None:
        raise HttpError(400, str(_("You must connect your Stripe account first.")))
    account = get_account_details(organization.stripe_account_id)
    organization.stripe_charges_enabled = account.charges_enabled
    organization.stripe_details_submitted = account.details_submitted
    organization.save()
    return organization


def _create_stripe_checkout_session(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    ticket: Ticket,
    effective_price: Decimal,
    application_fee_amount: int,
    expires_at: datetime,
) -> Session:
    """Create a Stripe Checkout Session.

    Args:
        event: The event for which the ticket is being purchased.
        tier: The ticket tier being purchased.
        user: The user purchasing the ticket.
        ticket: The pending ticket created for this purchase.
        effective_price: The final price for the ticket (after PWYC override).
        application_fee_amount: Platform fee in cents.
        expires_at: Session expiration timestamp.

    Returns:
        The created Stripe Checkout Session.

    Raises:
        HttpError: If Stripe API call fails.
    """
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    session_data = dict(  # noqa: C408
        customer_email=user.email,
        line_items=[
            {
                "price_data": {
                    "currency": tier.currency.lower(),
                    "product_data": {
                        "name": f"Ticket: {event.name} ({tier.name})",
                    },
                    "unit_amount": int(effective_price * 100),  # Amount in cents
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        success_url=f"{frontend_base_url}/events/{event.organization.slug}/{event.slug}?payment_success=true",
        cancel_url=f"{frontend_base_url}/events/{event.organization.slug}/{event.slug}?payment_cancelled=true",
        payment_intent_data={
            "application_fee_amount": application_fee_amount,
        },
        stripe_account=event.organization.stripe_account_id,
        metadata={
            "ticket_id": str(ticket.id),
            "event_id": str(event.id),
            "user_id": str(user.id),
        },
        expires_at=int(expires_at.timestamp()),
    )

    # If the organization is using the platform's own Stripe account,
    # remove connected account parameters (no fee to ourselves)
    if settings.STRIPE_ACCOUNT == event.organization.stripe_account_id:
        session_data.pop("stripe_account")
        session_data["payment_intent_data"].pop("application_fee_amount")  # type: ignore[union-attr]

    try:
        return Session.create(**session_data)  # type: ignore[arg-type]
    except Exception as e:
        raise HttpError(500, str(_("Stripe API error: {error}")).format(error=e)) from e


@transaction.atomic
def create_checkout_session(
    event: Event, tier: TicketTier, user: RevelUser, price_override: Decimal | None = None
) -> tuple[str, Payment]:
    """Create a Stripe Checkout Session for a ticket purchase."""
    if not event.organization.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))

    # Use price_override for PWYC, otherwise use tier.price
    effective_price = price_override if price_override is not None else tier.price

    if effective_price <= 0:
        raise HttpError(400, str(_("This ticket tier cannot be purchased.")))

    # Lock the tier for the entire transaction to safely check and update quantity
    locked_tier = TicketTier.objects.select_for_update().get(pk=tier.pk)

    if Ticket.objects.filter(~Q(status=Ticket.TicketStatus.PENDING), event=event, tier=locked_tier, user=user).exists():
        raise HttpError(400, str(_("You already have a ticket")))

    # Check if a pending ticket already exists for this user/tier combination
    existing_ticket = (
        Ticket.objects.filter(event=event, tier=locked_tier, user=user, status=Ticket.TicketStatus.PENDING)
        .select_related("payment")
        .first()
    )

    if existing_ticket and hasattr(existing_ticket, "payment"):
        payment = existing_ticket.payment
        if not payment.has_expired():
            # The user has an active session, retrieve it and send them back
            session = Session.retrieve(payment.stripe_session_id)
            return t.cast(str, session.url), payment
        else:
            payment.delete()
            existing_ticket.delete()
            TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") - 1)
            locked_tier.refresh_from_db()  # Reload the value after the F() update

    # Availability Check (after any potential cleanup)
    if locked_tier.total_quantity is not None and locked_tier.quantity_sold >= locked_tier.total_quantity:
        raise HttpError(429, str(_("This ticket tier is sold out.")))

    # Create a new pending ticket
    ticket = Ticket.objects.create(
        event=event,
        tier=locked_tier,
        user=user,
        status=Ticket.TicketStatus.PENDING,
        guest_name=user.get_display_name(),
    )

    platform_fee = round(effective_price * (event.organization.platform_fee_percent / Decimal(100)), 2)
    fixed_fee = event.organization.platform_fee_fixed
    application_fee_amount = int((platform_fee + fixed_fee) * 100)
    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)

    try:
        session = _create_stripe_checkout_session(
            event=event,
            tier=tier,
            user=user,
            ticket=ticket,
            effective_price=effective_price,
            application_fee_amount=application_fee_amount,
            expires_at=expires_at,
        )
    except HttpError:
        ticket.delete()
        raise

    # application_fee_amount is in cents.
    db_platform_fee = (Decimal(application_fee_amount) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payment = Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session.id,
        amount=effective_price,
        platform_fee=db_platform_fee,
        currency=tier.currency,
        status=Payment.PaymentStatus.PENDING,
        raw_response={},
        expires_at=expires_at,
    )
    TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + 1)

    return t.cast(str, session.url), payment


@transaction.atomic
def create_batch_checkout_session(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    tickets: list[Ticket],
    price_override: Decimal | None = None,
) -> str:
    """Create a Stripe Checkout Session for a batch ticket purchase.

    Args:
        event: The event for which tickets are being purchased.
        tier: The ticket tier being purchased.
        user: The user purchasing the tickets.
        tickets: List of PENDING tickets already created.
        price_override: Price override for PWYC tiers.

    Returns:
        The Stripe checkout URL.

    Raises:
        HttpError: If Stripe API call fails or organization not configured.
    """
    if not event.organization.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))

    # Use price_override for PWYC, otherwise use tier.price
    effective_price = price_override if price_override is not None else tier.price

    if effective_price <= 0:
        raise HttpError(400, str(_("This ticket tier cannot be purchased online.")))

    # Calculate fees
    total_amount = effective_price * len(tickets)
    platform_fee = round(total_amount * (event.organization.platform_fee_percent / Decimal(100)), 2)
    fixed_fee = event.organization.platform_fee_fixed
    application_fee_amount = int((platform_fee + fixed_fee) * 100)
    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)

    # Build line items - one per ticket with guest name
    line_items = [
        {
            "price_data": {
                "currency": tier.currency.lower(),
                "product_data": {
                    "name": f"Ticket: {event.name} ({tier.name})",
                    "description": f"Ticket for {ticket.guest_name}",
                },
                "unit_amount": int(effective_price * 100),
            },
            "quantity": 1,
        }
        for ticket in tickets
    ]

    # Build metadata with all ticket IDs
    ticket_ids = ",".join(str(_t.id) for _t in tickets)

    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    session_data = dict(  # noqa: C408
        customer_email=user.email,
        line_items=line_items,
        mode="payment",
        success_url=f"{frontend_base_url}/events/{event.organization.slug}/{event.slug}?payment_success=true",
        cancel_url=f"{frontend_base_url}/events/{event.organization.slug}/{event.slug}?payment_cancelled=true",
        payment_intent_data={
            "application_fee_amount": application_fee_amount,
        },
        stripe_account=event.organization.stripe_account_id,
        metadata={
            "ticket_ids": ticket_ids,
            "event_id": str(event.id),
            "user_id": str(user.id),
            "batch_size": str(len(tickets)),
        },
        expires_at=int(expires_at.timestamp()),
    )

    # If the organization is using the platform's own Stripe account,
    # remove connected account parameters
    if settings.STRIPE_ACCOUNT == event.organization.stripe_account_id:
        session_data.pop("stripe_account")
        session_data["payment_intent_data"].pop("application_fee_amount")  # type: ignore[union-attr]

    try:
        session = Session.create(**session_data)  # type: ignore[arg-type]
    except Exception as e:
        # Delete the tickets if session creation fails
        Ticket.objects.filter(id__in=[_t.id for _t in tickets]).delete()
        raise HttpError(500, str(_("Stripe API error: {error}")).format(error=e)) from e

    # Create Payment records for each ticket
    db_platform_fee_per_ticket = (Decimal(application_fee_amount) / Decimal(100) / len(tickets)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    payments = [
        Payment(
            ticket=ticket,
            user=user,
            stripe_session_id=session.id,
            amount=effective_price,
            platform_fee=db_platform_fee_per_ticket,
            currency=tier.currency,
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
            expires_at=expires_at,
        )
        for ticket in tickets
    ]
    Payment.objects.bulk_create(payments)

    return t.cast(str, session.url)


@transaction.atomic
def _cleanup_expired_batch(payment: Payment) -> None:
    """Clean up an expired payment batch (all tickets with same session_id)."""
    batch_payments = Payment.objects.filter(stripe_session_id=payment.stripe_session_id)
    ticket_count = batch_payments.count()
    ticket_ids = list(batch_payments.values_list("ticket_id", flat=True))

    # Clean up all tickets and payments in this batch
    batch_payments.delete()
    Ticket.objects.filter(id__in=ticket_ids).delete()

    # Decrement quantity sold
    tier = payment.ticket.tier
    if tier:
        TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=F("quantity_sold") - ticket_count)


def resume_pending_checkout(
    payment_id: str,
    user: RevelUser,
) -> str:
    """Resume a pending Stripe checkout session by payment ID.

    Retrieves the existing Stripe checkout URL for a pending payment.
    Cleans up expired sessions and all tickets in the same batch.

    Args:
        payment_id: The UUID of the pending payment.
        user: The user who initiated the purchase.

    Returns:
        The Stripe checkout URL.

    Raises:
        HttpError: 404 if payment not found, not owned by user, or session expired.
    """
    # Find the payment and verify ownership
    payment = (
        Payment.objects.filter(
            id=payment_id,
            user=user,
            status=Payment.PaymentStatus.PENDING,
        )
        .select_related("ticket__event__organization", "ticket__tier")
        .first()
    )

    if not payment:
        raise HttpError(404, str(_("No pending payment found.")))

    event = payment.ticket.event

    # Check if the payment has expired - cleanup commits in its own transaction
    if payment.has_expired():
        _cleanup_expired_batch(payment)
        raise HttpError(404, str(_("Checkout session has expired. Please start a new purchase.")))

    # Retrieve and return the Stripe session URL
    try:
        session = Session.retrieve(
            payment.stripe_session_id,
            stripe_account=event.organization.stripe_account_id
            if event.organization.stripe_account_id != settings.STRIPE_ACCOUNT
            else None,
        )
        if session.url:
            return session.url
        raise HttpError(404, str(_("Checkout session is no longer valid.")))
    except stripe.error.InvalidRequestError:
        raise HttpError(404, str(_("Checkout session not found.")))


@transaction.atomic
def cancel_pending_checkout(
    payment_id: str,
    user: RevelUser,
) -> int:
    """Cancel a pending Stripe checkout and delete associated tickets.

    Deletes all payments and tickets in the same batch (same stripe_session_id).

    Args:
        payment_id: The UUID of the pending payment to cancel.
        user: The user who owns the payment.

    Returns:
        Number of tickets cancelled.

    Raises:
        HttpError: 404 if payment not found or not owned by user.
        HttpError: 400 if payment is not in PENDING status.
    """
    # Find the payment and verify ownership
    payment = (
        Payment.objects.filter(
            id=payment_id,
            user=user,
        )
        .select_related("ticket__tier")
        .first()
    )

    if not payment:
        raise HttpError(404, str(_("Payment not found.")))

    if payment.status != Payment.PaymentStatus.PENDING:
        raise HttpError(400, str(_("Only pending payments can be cancelled.")))

    # Find all payments in this batch (same session_id)
    batch_payments = Payment.objects.filter(stripe_session_id=payment.stripe_session_id)
    ticket_count = batch_payments.count()
    ticket_ids = list(batch_payments.values_list("ticket_id", flat=True))

    # Delete all tickets and payments in this batch
    batch_payments.delete()
    Ticket.objects.filter(id__in=ticket_ids).delete()

    # Decrement quantity sold
    tier = payment.ticket.tier
    if tier:
        TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=F("quantity_sold") - ticket_count)

    logger.info(
        "pending_checkout_cancelled",
        payment_id=payment_id,
        user_id=str(user.id),
        tickets_cancelled=ticket_count,
    )

    return ticket_count


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
