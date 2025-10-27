import logging
import typing as t
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import stripe
from django.conf import settings
from django.db import transaction
from common.models import SiteSettings
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja.errors import HttpError
from stripe.checkout import Session

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.tasks import send_payment_confirmation_email

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def create_connect_account(organization: Organization) -> str:
    """Create a Stripe Connect Standard account for an organization."""
    account = stripe.Account.create(type="standard", email=organization.owner.email)
    organization.stripe_account_id = account.id
    organization.save(update_fields=["stripe_account_id"])
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
        raise HttpError(400, "You must connect your Stripe account first.")
    account = get_account_details(organization.stripe_account_id)
    organization.stripe_charges_enabled = account.charges_enabled
    organization.stripe_details_submitted = account.details_submitted
    organization.save()
    return organization


@transaction.atomic
def create_checkout_session(
    event: Event, tier: TicketTier, user: RevelUser, price_override: Decimal | None = None
) -> tuple[str, Payment]:
    """Create a Stripe Checkout Session for a ticket purchase."""
    if not event.organization.is_stripe_connected:
        raise HttpError(400, "This organization is not configured to accept payments.")

    # Use price_override for PWYC, otherwise use tier.price
    effective_price = price_override if price_override is not None else tier.price

    if effective_price <= 0:
        raise HttpError(400, "This ticket tier cannot be purchased.")

    # Lock the tier for the entire transaction to safely check and update quantity
    locked_tier = TicketTier.objects.select_for_update().get(pk=tier.pk)

    if Ticket.objects.filter(~Q(status=Ticket.Status.PENDING), event=event, tier=locked_tier, user=user).exists():
        raise HttpError(400, "You already have a ticket")

    # Check if a pending ticket already exists for this user/tier combination
    existing_ticket = (
        Ticket.objects.filter(event=event, tier=locked_tier, user=user, status=Ticket.Status.PENDING)
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
        raise HttpError(429, "This ticket tier is sold out.")

    # Create a new pending ticket
    ticket = Ticket.objects.create(event=event, tier=locked_tier, user=user, status=Ticket.Status.PENDING)

    platform_fee = round(effective_price * (event.organization.platform_fee_percent / Decimal(100)), 2)
    fixed_fee = event.organization.platform_fee_fixed
    application_fee_amount = int((platform_fee + fixed_fee) * 100)
    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    try:
        session = Session.create(
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
    except Exception as e:
        ticket.delete()
        raise HttpError(500, f"Stripe API error: {e}")

    # application_fee_amount is in cents.
    db_platform_fee = (Decimal(application_fee_amount) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payment = Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session.id,
        amount=effective_price,
        platform_fee=db_platform_fee,
        currency=tier.currency,
        status=Payment.Status.PENDING,
        raw_response={},
        expires_at=expires_at,
    )
    TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + 1)

    return t.cast(str, session.url), payment


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
        logger.info(f"Unhandled Stripe event type received: {event.type}")

    @transaction.atomic
    def handle_checkout_session_completed(self, event: stripe.Event) -> None:
        """Handles the successful completion of a checkout session.

        Updates payment and ticket status and triggers confirmation email.
        """
        session = event.data.object
        session_id = session["id"]
        payment = get_object_or_404(Payment, stripe_session_id=session_id)

        if session["payment_status"] not in {"paid", "no_payment_required"}:
            logger.warning(
                f"Session {session_id} completed with unresolved payment_status: {session['payment_status']}. Skipping."
            )
            return

        if payment.status == Payment.Status.SUCCEEDED:
            logger.warning(f"Webhook for already succeeded payment {payment.id} received. Ignoring.")
            return  # Webhook already processed, idempotent

        payment.status = Payment.Status.SUCCEEDED
        payment.stripe_payment_intent_id = session.get("payment_intent")
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "stripe_payment_intent_id", "raw_response"])

        ticket = payment.ticket
        ticket.status = Ticket.Status.ACTIVE
        ticket.save(update_fields=["status"])

        # Send payment confirmation email (includes PDF and ICS attachments)
        send_payment_confirmation_email.delay(str(payment.id))
        logger.info(f"Successfully processed checkout.session.completed for Payment ID: {payment.id}")

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
            logger.warning(f"Received account.updated for unknown Stripe account: {account_id}")
            return

        # Update the organization's Stripe status
        organization.stripe_charges_enabled = account_data.get("charges_enabled", False)
        organization.stripe_details_submitted = account_data.get("details_submitted", False)
        organization.save(update_fields=["stripe_charges_enabled", "stripe_details_submitted"])

        logger.info(
            f"Updated Stripe status for organization {organization.slug}: "
            f"charges_enabled={organization.stripe_charges_enabled}, "
            f"details_submitted={organization.stripe_details_submitted}"
        )

    @transaction.atomic
    def handle_charge_refunded(self, event: stripe.Event) -> None:
        """Handle refund events from Stripe.

        When a connected account issues a refund (via Dashboard or API),
        this webhook updates the payment and ticket status.
        Stripe automatically refunds application fees proportionally.
        """
        charge_data = event.data.object
        payment_intent_id = charge_data.get("payment_intent")

        if not payment_intent_id:
            logger.warning(f"Received charge.refunded without payment_intent: {charge_data.get('id')}")
            return

        # Find the payment by payment_intent_id
        try:
            payment = Payment.objects.select_related("ticket", "ticket__tier").get(
                stripe_payment_intent_id=payment_intent_id
            )
        except Payment.DoesNotExist:
            logger.warning(f"Received charge.refunded for unknown payment_intent: {payment_intent_id}")
            return

        # Idempotency check
        if payment.status == Payment.Status.REFUNDED:
            logger.warning(f"Webhook for already refunded payment {payment.id} received. Ignoring.")
            return

        # Update payment status
        payment.status = Payment.Status.REFUNDED
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "raw_response"])

        # Cancel the ticket
        ticket = payment.ticket
        ticket.status = Ticket.Status.CANCELLED
        ticket.save(update_fields=["status"])

        # Restore ticket quantity
        from django.db.models import F

        TicketTier.objects.filter(pk=ticket.tier.pk).update(quantity_sold=F("quantity_sold") - 1)

        logger.info(
            f"Processed refund for Payment {payment.id}: "
            f"Payment set to REFUNDED, Ticket {ticket.id} set to CANCELLED, quantity restored"
        )

    @transaction.atomic
    def handle_payment_intent_canceled(self, event: stripe.Event) -> None:
        """Handle canceled payment intents.

        This fires when a payment is canceled before being captured.
        For example, when a checkout session expires without payment.
        """
        payment_intent_data = event.data.object
        payment_intent_id = payment_intent_data.get("id")

        if not payment_intent_id:
            logger.warning("Received payment_intent.canceled without id")
            return

        # Find the payment by payment_intent_id
        try:
            payment = Payment.objects.select_related("ticket", "ticket__tier").get(
                stripe_payment_intent_id=payment_intent_id
            )
        except Payment.DoesNotExist:
            # This is expected for sessions that expire naturally before payment
            logger.debug(f"Received payment_intent.canceled for unknown payment_intent: {payment_intent_id}")
            return

        # Only update if payment is still pending
        if payment.status != Payment.Status.PENDING:
            logger.info(
                f"Received payment_intent.canceled for non-pending payment {payment.id} "
                f"(status: {payment.status}). Ignoring."
            )
            return

        # Update payment status to failed (canceled before capture)
        payment.status = Payment.Status.FAILED
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "raw_response"])

        # Cancel the ticket
        ticket = payment.ticket
        ticket.status = Ticket.Status.CANCELLED
        ticket.save(update_fields=["status"])

        # Restore ticket quantity
        from django.db.models import F

        TicketTier.objects.filter(pk=ticket.tier.pk).update(quantity_sold=F("quantity_sold") - 1)

        logger.info(
            f"Processed payment_intent.canceled for Payment {payment.id}: "
            f"Payment set to FAILED, Ticket {ticket.id} set to CANCELLED, quantity restored"
        )
