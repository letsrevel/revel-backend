import logging
import typing as t
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import stripe
from django.conf import settings
from django.db import transaction
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
    refresh_url = f"{settings.FRONTEND_BASE_URL}/dashboard/org/{organization.slug}/settings?stripe_refresh=true"
    return_url = f"{settings.FRONTEND_BASE_URL}/dashboard/org/{organization.slug}/settings?stripe_success=true"
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
            success_url=f"{settings.FRONTEND_BASE_URL}/events/{event.organization.slug}/{event.slug}?payment_success=true",
            cancel_url=f"{settings.FRONTEND_BASE_URL}/events/{event.organization.slug}/{event.slug}?payment_cancelled=true",
            payment_intent_data={
                "application_fee_amount": application_fee_amount,
                "transfer_data": {
                    "destination": event.organization.stripe_account_id,  # type: ignore[typeddict-item]
                },
            },
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
        payment.raw_response = dict(event)
        payment.save(update_fields=["status", "raw_response"])

        ticket = payment.ticket
        ticket.status = Ticket.Status.ACTIVE
        ticket.save(update_fields=["status"])

        # Send payment confirmation email (includes PDF and ICS attachments)
        send_payment_confirmation_email.delay(str(payment.id))
        logger.info(f"Successfully processed checkout.session.completed for Payment ID: {payment.id}")
