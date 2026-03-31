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
from common.service.exchange_rate_service import convert as convert_currency
from common.service.stripe_connect_service import (
    create_account_link as _create_account_link,
)
from common.service.stripe_connect_service import (
    create_connect_account as _create_connect_account,
)
from common.service.stripe_connect_service import (
    get_account_details as get_account_details,
)
from common.service.stripe_connect_service import (
    sync_account_status,
)
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.models.attendee_invoice import BuyerBillingSnapshot
from events.service.vat_service import (
    calculate_platform_fee_vat,
    calculate_vat_inclusive,
    distribute_amount_across_items,
    get_effective_vat_rate,
)

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema
    from events.service.attendee_vat_service import AttendeeVATResult

logger = structlog.get_logger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def create_connect_account(organization: Organization, stripe_account_email: EmailStr) -> str:
    """Create a Stripe Connect Standard account for an organization."""
    return _create_connect_account(organization, stripe_account_email, account_type="standard")


def create_account_link(account_id: str, organization: Organization) -> str:
    """Create a one-time onboarding link for an organization's Stripe Connect account."""
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    refresh_url = f"{frontend_base_url}/org/{organization.slug}/admin/settings?stripe_refresh=true"
    return_url = f"{frontend_base_url}/org/{organization.slug}/admin/settings?stripe_success=true"
    return _create_account_link(account_id, refresh_url, return_url)


def stripe_verify_account(organization: Organization) -> Organization:
    """Verify a Stripe Connect account.

    Also auto-fills billing_address and vat_country_code from Stripe account
    details if they are currently empty (fallback for orgs without a VAT ID).
    """
    account = sync_account_status(organization)

    # Organization-specific: auto-fill billing details from Stripe
    update_fields: list[str] = []

    if not organization.billing_address and account.get("company"):
        company = account["company"]
        address = company.get("address", {})
        parts = [
            address.get("line1", ""),
            address.get("line2", ""),
            address.get("postal_code", ""),
            address.get("city", ""),
            address.get("state", ""),
            address.get("country", ""),
        ]
        full_address = ", ".join(p for p in parts if p)
        if full_address:
            organization.billing_address = full_address
            update_fields.append("billing_address")

    if not organization.vat_country_code and account.get("country"):
        organization.vat_country_code = account["country"]
        update_fields.append("vat_country_code")

    if update_fields:
        update_fields.append("updated_at")
        organization.save(update_fields=update_fields)

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
        session_data["payment_intent_data"].pop("application_fee_amount")  # type: ignore[union-attr, arg-type]

    try:
        return Session.create(**session_data)  # type: ignore[arg-type]
    except Exception as e:
        logger.error("stripe_session_creation_failed", error=str(e), event_id=str(event.id))
        raise HttpError(500, str(_("Payment processing failed. Please try again later."))) from e


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

    org = event.organization
    net_fee = (effective_price * org.platform_fee_percent / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    # Fixed fee is stored in DEFAULT_CURRENCY; convert to payment currency if different.
    # Exchange rates are always available (seeded by migration, refreshed daily).
    # Unsupported currencies are rejected at tier creation (schema validation).
    fixed_fee = convert_currency(org.platform_fee_fixed, settings.DEFAULT_CURRENCY, tier.currency)
    net_fee_total = net_fee + fixed_fee

    # Gross up the net fee with VAT when applicable
    site = SiteSettings.get_solo()
    fee_vat = calculate_platform_fee_vat(net_fee_total, org, site.platform_vat_country, site.platform_vat_rate)
    application_fee_amount = int(fee_vat.fee_gross * 100)

    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)

    session = _create_stripe_checkout_session(
        event=event,
        tier=tier,
        user=user,
        ticket=ticket,
        effective_price=effective_price,
        application_fee_amount=application_fee_amount,
        expires_at=expires_at,
    )

    # Ticket sale VAT breakdown
    effective_vat_rate = get_effective_vat_rate(tier.vat_rate, org.vat_rate)
    ticket_vat = calculate_vat_inclusive(effective_price, effective_vat_rate)

    payment = Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session.id,
        amount=effective_price,
        platform_fee=fee_vat.fee_gross,
        currency=tier.currency,
        status=Payment.PaymentStatus.PENDING,
        raw_response={},
        expires_at=expires_at,
        # Ticket sale VAT breakdown
        net_amount=ticket_vat.net_amount,
        vat_amount=ticket_vat.vat_amount,
        vat_rate=ticket_vat.vat_rate,
        # Platform fee VAT breakdown
        platform_fee_net=fee_vat.fee_net,
        platform_fee_vat=fee_vat.fee_vat,
        platform_fee_vat_rate=fee_vat.fee_vat_rate,
        platform_fee_reverse_charge=fee_vat.reverse_charge,
    )
    TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + 1)

    return t.cast(str, session.url), payment


def _build_billing_snapshot(
    billing_info: "BuyerBillingInfoSchema",
    vat_id_validated: bool,
    reverse_charge: bool,
) -> BuyerBillingSnapshot:
    """Build a buyer billing snapshot dict from checkout billing info."""
    return BuyerBillingSnapshot(
        billing_name=billing_info.billing_name,
        vat_id=billing_info.vat_id,
        vat_country_code=billing_info.vat_country_code,
        vat_id_validated=vat_id_validated,
        billing_address=billing_info.billing_address,
        billing_email=billing_info.billing_email,
        reverse_charge=reverse_charge,
    )


def _save_billing_to_profile(user: RevelUser, billing_info: "BuyerBillingInfoSchema") -> None:
    """Save buyer billing info to the user's billing profile."""
    from accounts.models import UserBillingProfile

    profile, _ = UserBillingProfile.objects.get_or_create(user=user)
    fields_to_update = ["billing_name", "billing_address", "billing_email"]
    profile.billing_name = billing_info.billing_name
    profile.billing_address = billing_info.billing_address
    profile.billing_email = billing_info.billing_email
    if billing_info.vat_id:
        profile.vat_id = billing_info.vat_id
        profile.vat_country_code = billing_info.vat_country_code
        fields_to_update.extend(["vat_id", "vat_country_code"])
    profile.save(update_fields=fields_to_update)


def _maybe_resolve_attendee_vat(
    billing_info: "BuyerBillingInfoSchema | None",
    tier: TicketTier,
    org: Organization,
    base_price: Decimal,
) -> "tuple[AttendeeVATResult | None, bool]":
    """Resolve attendee VAT if billing info is provided with a buyer country.

    Uses the shared validate_and_resolve_buyer_country helper for VIES
    validation and country derivation. Returns None (no VAT adjustment)
    when no buyer country can be determined.

    Returns:
        Tuple of (AttendeeVATResult or None, buyer_vat_validated).
    """
    from events.service.attendee_vat_service import (
        determine_attendee_vat,
        validate_and_resolve_buyer_country,
    )
    from events.service.attendee_vat_service import get_effective_vat_rate as get_vat_rate

    if not billing_info:
        return None, False

    vat_id_valid, _, buyer_country = validate_and_resolve_buyer_country(
        vat_id=billing_info.vat_id,
        vat_country_code=billing_info.vat_country_code,
    )
    buyer_vat_validated = bool(vat_id_valid)

    if not buyer_country:
        return None, False

    seller_vat_rate = get_vat_rate(tier, org)
    vat_result = determine_attendee_vat(
        gross_price=base_price,
        seller_vat_rate=seller_vat_rate,
        seller_country=org.vat_country_code,
        buyer_country=buyer_country,
        buyer_vat_id_valid=buyer_vat_validated,
    )
    return vat_result, buyer_vat_validated


@transaction.atomic
def create_batch_checkout_session(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    tickets: list[Ticket],
    price_override: Decimal | None = None,
    billing_info: "BuyerBillingInfoSchema | None" = None,
) -> str:
    """Create a Stripe Checkout Session for a batch ticket purchase.

    Args:
        event: The event for which tickets are being purchased.
        tier: The ticket tier being purchased.
        user: The user purchasing the tickets.
        tickets: List of PENDING tickets already created.
        price_override: Price override for PWYC tiers.
        billing_info: Optional buyer billing info for attendee invoicing.

    Returns:
        The Stripe checkout URL.

    Raises:
        HttpError: If Stripe API call fails or organization not configured.
    """
    if not event.organization.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))

    # Use price_override for PWYC, otherwise use tier.price
    base_price = price_override if price_override is not None else tier.price

    if base_price <= 0:
        raise HttpError(400, str(_("This ticket tier cannot be purchased online.")))

    org = event.organization

    # Determine VAT treatment based on buyer billing info
    attendee_vat_result, buyer_vat_validated = _maybe_resolve_attendee_vat(billing_info, tier, org, base_price)

    # The price the buyer actually pays (may be reduced for reverse charge / export)
    effective_price = attendee_vat_result.effective_price if attendee_vat_result else base_price

    # Calculate net fee and gross up with VAT
    total_amount = effective_price * len(tickets)
    net_fee = (total_amount * org.platform_fee_percent / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Fixed fee is stored in DEFAULT_CURRENCY; convert to payment currency if different.
    # Exchange rates are always available (seeded by migration, refreshed daily).
    # Unsupported currencies are rejected at tier creation (schema validation).
    fixed_fee = convert_currency(org.platform_fee_fixed, settings.DEFAULT_CURRENCY, tier.currency)
    net_fee_total = net_fee + fixed_fee

    site = SiteSettings.get_solo()
    total_fee_vat = calculate_platform_fee_vat(net_fee_total, org, site.platform_vat_country, site.platform_vat_rate)
    application_fee_amount = int(total_fee_vat.fee_gross * 100)

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

    frontend_base_url = site.frontend_base_url
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
        session_data["payment_intent_data"].pop("application_fee_amount")  # type: ignore[union-attr, arg-type]

    try:
        session = Session.create(**session_data)  # type: ignore[arg-type]
    except Exception as e:
        logger.error("stripe_batch_session_creation_failed", error=str(e), event_id=str(event.id))
        raise HttpError(500, str(_("Payment processing failed. Please try again later."))) from e

    # Distribute gross and vat independently; derive net = gross - vat.
    # This guarantees non-negative per-ticket VAT (unlike distributing gross + net
    # independently, where remainder pennies could land on different indices).
    ticket_count = len(tickets)
    per_ticket_gross = distribute_amount_across_items(total_fee_vat.fee_gross, ticket_count)
    per_ticket_vat = distribute_amount_across_items(total_fee_vat.fee_vat, ticket_count)

    # Ticket sale VAT breakdown
    if attendee_vat_result:
        ticket_net = attendee_vat_result.net_amount
        ticket_vat_amount = attendee_vat_result.vat_amount
        ticket_vat_rate = attendee_vat_result.vat_rate
    else:
        effective_vat_rate = get_effective_vat_rate(tier.vat_rate, org.vat_rate)
        ticket_vat = calculate_vat_inclusive(effective_price, effective_vat_rate)
        ticket_net = ticket_vat.net_amount
        ticket_vat_amount = ticket_vat.vat_amount
        ticket_vat_rate = ticket_vat.vat_rate

    # Build buyer billing snapshot if billing info was provided
    billing_snapshot: BuyerBillingSnapshot | None = None
    if billing_info:
        is_reverse_charge = attendee_vat_result.reverse_charge if attendee_vat_result else False
        billing_snapshot = _build_billing_snapshot(billing_info, buyer_vat_validated, is_reverse_charge)

    payments = [
        Payment(
            ticket=ticket,
            user=user,
            stripe_session_id=session.id,
            amount=effective_price,
            platform_fee=per_ticket_gross[i],
            currency=tier.currency,
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
            expires_at=expires_at,
            # Ticket sale VAT breakdown
            net_amount=ticket_net,
            vat_amount=ticket_vat_amount,
            vat_rate=ticket_vat_rate,
            # Platform fee VAT breakdown (distributed to avoid penny errors)
            platform_fee_net=per_ticket_gross[i] - per_ticket_vat[i],
            platform_fee_vat=per_ticket_vat[i],
            platform_fee_vat_rate=total_fee_vat.fee_vat_rate,
            platform_fee_reverse_charge=total_fee_vat.reverse_charge,
            # Buyer billing snapshot for attendee invoicing
            buyer_billing_snapshot=billing_snapshot,
        )
        for i, ticket in enumerate(tickets)
    ]
    Payment.objects.bulk_create(payments)

    # Save billing info to user profile if requested
    if billing_info and billing_info.save_to_profile:
        _save_billing_to_profile(user, billing_info)

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
