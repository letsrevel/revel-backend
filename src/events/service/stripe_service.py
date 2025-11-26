import typing as t
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import stripe
import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
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
    ticket = Ticket.objects.create(event=event, tier=locked_tier, user=user, status=Ticket.TicketStatus.PENDING)

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
        """
        session = event.data.object
        session_id = session["id"]
        payment = get_object_or_404(Payment, stripe_session_id=session_id)

        if session["payment_status"] not in {"paid", "no_payment_required"}:
            logger.warning(
                "stripe_session_unresolved_payment",
                session_id=session_id,
                payment_status=session["payment_status"],
            )
            return

        if payment.status == Payment.PaymentStatus.SUCCEEDED:
            logger.warning("stripe_webhook_duplicate_payment_success", payment_id=str(payment.id))
            return  # Webhook already processed, idempotent

        payment.status = Payment.PaymentStatus.SUCCEEDED
        payment.stripe_payment_intent_id = session.get("payment_intent")
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "stripe_payment_intent_id", "raw_response"])

        ticket = payment.ticket
        # Store original status so signal handler can detect PENDING→ACTIVE transition
        ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
        ticket.status = Ticket.TicketStatus.ACTIVE
        ticket.save(update_fields=["status"])

        # Notifications are now handled by Payment post_save signal in notifications/signals/payment.py
        logger.info(
            "stripe_payment_success",
            payment_id=str(payment.id),
            ticket_id=str(ticket.id),
            amount=float(payment.amount),
            currency=payment.currency,
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
        """
        charge_data = event.data.object
        payment_intent_id = charge_data.get("payment_intent")

        if not payment_intent_id:
            logger.warning("stripe_refund_missing_intent", charge_id=charge_data.get("id"))
            return

        # Find the payment by payment_intent_id
        try:
            payment = Payment.objects.select_related("ticket", "ticket__tier").get(
                stripe_payment_intent_id=payment_intent_id
            )
        except Payment.DoesNotExist:
            logger.warning("stripe_refund_unknown_intent", payment_intent_id=payment_intent_id)
            return

        # Idempotency check
        if payment.status == Payment.PaymentStatus.REFUNDED:
            logger.warning("stripe_webhook_duplicate_refund", payment_id=str(payment.id))
            return

        # Update payment status
        payment.status = Payment.PaymentStatus.REFUNDED
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "raw_response"])

        # Cancel the ticket
        # Notifications are now handled by Payment post_save signal in notifications/signals/payment.py
        ticket = payment.ticket
        ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
        ticket._refund_amount = f"{payment.amount} {payment.currency}"  # type: ignore[attr-defined]
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save(update_fields=["status"])

        # Restore ticket quantity
        TicketTier.objects.filter(pk=ticket.tier.pk).update(quantity_sold=F("quantity_sold") - 1)

        logger.info(
            "stripe_refund_processed",
            payment_id=str(payment.id),
            ticket_id=str(ticket.id),
            amount=float(payment.amount),
            currency=payment.currency,
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
            logger.warning("stripe_payment_intent_canceled_missing_id")
            return

        # Find the payment by payment_intent_id
        try:
            payment = Payment.objects.select_related("ticket", "ticket__tier").get(
                stripe_payment_intent_id=payment_intent_id
            )
        except Payment.DoesNotExist:
            # This is expected for sessions that expire naturally before payment
            logger.debug("stripe_payment_intent_canceled_unknown", payment_intent_id=payment_intent_id)
            return

        # Only update if payment is still pending
        if payment.status != Payment.PaymentStatus.PENDING:
            logger.info(
                "stripe_payment_intent_canceled_non_pending",
                payment_id=str(payment.id),
                status=payment.status,
            )
            return

        # Update payment status to failed (canceled before capture)
        payment.status = Payment.PaymentStatus.FAILED
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "raw_response"])

        # Cancel the ticket
        ticket = payment.ticket
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save(update_fields=["status"])

        # Restore ticket quantity
        TicketTier.objects.filter(pk=ticket.tier.pk).update(quantity_sold=F("quantity_sold") - 1)

        logger.info(
            "stripe_payment_intent_canceled_processed",
            payment_id=str(payment.id),
            ticket_id=str(ticket.id),
        )
