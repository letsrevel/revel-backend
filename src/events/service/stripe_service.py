import typing as t
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

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
from events.models import Event, HeldSeriesPass, Organization, Payment, Ticket, TicketTier
from events.models.attendee_invoice import BuyerBillingSnapshot

# Re-exported: the pending-checkout batch cleanup lives in its own module (file-length
# limit) but callers and tests keep addressing it via the stripe_service namespace.
from events.service.pending_checkout import (
    _cleanup_expired_batch as _cleanup_expired_batch,
)
from events.service.pending_checkout import (
    _release_batch_tier_capacity as _release_batch_tier_capacity,
)
from events.service.pending_checkout import (
    cancel_pending_checkout as cancel_pending_checkout,
)
from events.service.pending_checkout import (
    resume_pending_checkout as resume_pending_checkout,
)
from events.service.vat_service import (
    calculate_platform_fee_vat,
    calculate_vat_inclusive,
    distribute_amount_across_items,
    get_effective_vat_rate,
)
from events.utils.currency import to_stripe_amount

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema
    from events.service.attendee_vat_service import AttendeeVATResult
    from events.service.vat_service import PlatformFeeVATBreakdown as PlatformFeeVATResult

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time. The pinned version
# guards outbound response shapes against silent changes when the stripe SDK
# (whose default version tracks its release) gets bumped by a `uv sync`.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


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
                    "unit_amount": to_stripe_amount(effective_price, tier.currency),
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

    # Reuse an active pending session if one exists (otherwise stale ones are cleared).
    reused = _reuse_or_clear_pending_ticket(event, locked_tier, user)
    if reused is not None:
        return reused

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
    fee_vat = _compute_platform_fee_vat(org, effective_price, tier.currency)
    application_fee_amount = to_stripe_amount(fee_vat.fee_gross, tier.currency)

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

    payment = _create_single_payment_record(
        ticket=ticket,
        user=user,
        session_id=session.id,
        tier=tier,
        effective_price=effective_price,
        fee_vat=fee_vat,
        expires_at=expires_at,
        org=org,
    )
    TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + 1)

    return t.cast(str, session.url), payment


def _reuse_or_clear_pending_ticket(
    event: Event,
    locked_tier: TicketTier,
    user: RevelUser,
) -> tuple[str, Payment] | None:
    """Return an active pending checkout to resume, or clear a stale one.

    Must run inside ``create_checkout_session``'s atomic block with the tier locked.

    Returns:
        ``(checkout_url, payment)`` when the user has an unexpired pending session
        to resume; ``None`` otherwise (after deleting any expired pending ticket and
        decrementing the tier's ``quantity_sold``).
    """
    existing_ticket = (
        Ticket.objects.filter(event=event, tier=locked_tier, user=user, status=Ticket.TicketStatus.PENDING)
        .select_related("payment")
        .first()
    )

    if not (existing_ticket and hasattr(existing_ticket, "payment")):
        return None

    payment = existing_ticket.payment
    if not payment.has_expired():
        # The user has an active session, retrieve it and send them back.
        # Connected-account sessions must be retrieved with the same stripe_account
        # they were created with, otherwise Stripe raises InvalidRequestError (mirrors
        # the retrieval in resume_pending_checkout).
        #
        # No InvalidRequestError guard here (unlike resume_pending_checkout): a not-found
        # retrieve is unreachable on this path. create_checkout_session's is_stripe_connected
        # precheck rejects deauthorized orgs before we get here, expired sessions are gated by
        # has_expired() (and stay retrievable anyway), and the account context now matches how
        # the session was created. resume_pending_checkout needs the guard because it has no
        # such precheck.
        org_stripe_account = event.organization.stripe_account_id
        session = Session.retrieve(
            payment.stripe_session_id,
            stripe_account=org_stripe_account if org_stripe_account != settings.STRIPE_ACCOUNT else None,
        )
        return t.cast(str, session.url), payment

    payment.delete()
    existing_ticket.delete()
    TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") - 1)
    locked_tier.refresh_from_db()  # Reload the value after the F() update
    return None


def _compute_platform_fee_vat(
    org: Organization,
    amount: Decimal,
    currency: str,
) -> "PlatformFeeVATResult":
    """Compute the platform fee (percent + fixed) and gross it up with VAT.

    The fixed fee is stored in ``DEFAULT_CURRENCY`` and converted to ``currency``.
    Exchange rates are always available (seeded by migration, refreshed daily);
    unsupported currencies are rejected at tier creation (schema validation).
    """
    net_fee = (amount * org.platform_fee_percent / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fixed_fee = convert_currency(org.platform_fee_fixed, settings.DEFAULT_CURRENCY, currency)
    net_fee_total = net_fee + fixed_fee

    site = SiteSettings.get_solo()
    return calculate_platform_fee_vat(net_fee_total, org, site.platform_vat_country, site.platform_vat_rate)


def _create_single_payment_record(
    *,
    ticket: Ticket,
    user: RevelUser,
    session_id: str,
    tier: TicketTier,
    effective_price: Decimal,
    fee_vat: "PlatformFeeVATResult",
    expires_at: datetime,
    org: Organization,
) -> Payment:
    """Create the Payment row for a single-ticket checkout, with VAT breakdown."""
    effective_vat_rate = get_effective_vat_rate(tier.vat_rate, org.vat_rate)
    ticket_vat = calculate_vat_inclusive(effective_price, effective_vat_rate)

    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session_id,
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


def _build_line_items(
    tickets: list[Ticket],
    event: Event,
    tier: TicketTier,
    effective_price: Decimal,
) -> list[dict[str, object]]:
    """Build Stripe line items — one per ticket with guest name."""
    return [
        {
            "price_data": {
                "currency": tier.currency.lower(),
                "product_data": {
                    "name": f"Ticket: {event.name} ({tier.name})",
                    "description": f"Ticket for {ticket.guest_name}",
                },
                "unit_amount": to_stripe_amount(effective_price, tier.currency),
            },
            "quantity": 1,
        }
        for ticket in tickets
    ]


def _create_stripe_session(
    *,
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    tickets: list[Ticket],
    effective_price: Decimal,
    application_fee_amount: int,
    expires_at: datetime,
    site: SiteSettings,
    idempotency_key: str | None = None,
) -> Session:
    """Build session data and create a Stripe Checkout Session.

    ``idempotency_key`` (e.g. a reservation id) makes retries of an interrupted
    session-create call reuse the same Stripe session instead of double-charging
    (#632); callers with nothing to key on may omit it.

    Returns:
        The created Stripe Session object.

    Raises:
        HttpError: If Stripe API call fails.
    """
    line_items = _build_line_items(tickets, event, tier, effective_price)
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
        return Session.create(**session_data, idempotency_key=idempotency_key)  # type: ignore[arg-type]
    except Exception as e:
        logger.error("stripe_batch_session_creation_failed", error=str(e), event_id=str(event.id))
        raise HttpError(500, str(_("Payment processing failed. Please try again later."))) from e


def _create_payment_records(
    *,
    tickets: list[Ticket],
    user: RevelUser,
    session_id: str,
    tier: TicketTier,
    effective_price: Decimal,
    total_fee_vat: "PlatformFeeVATResult",
    attendee_vat_result: "AttendeeVATResult | None",
    billing_info: "BuyerBillingInfoSchema | None",
    buyer_vat_validated: bool,
    expires_at: datetime,
    org: Organization,
    reservation_id: UUID,
) -> None:
    """Build and bulk-create Payment records for a batch checkout."""
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
            stripe_session_id=session_id,
            reservation_id=reservation_id,
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


def resolve_attendee_vat_for_reserve(
    *,
    tier: TicketTier,
    org: Organization,
    price_override: Decimal | None = None,
    billing_info: "BuyerBillingInfoSchema | None" = None,
) -> "tuple[AttendeeVATResult | None, bool]":
    """Resolve attendee VAT (runs VIES) for a batch reserve.

    Called by BatchTicketService.create_batch BEFORE it takes the TicketTier
    select_for_update, so the contended row is never locked across the VIES
    round-trip (#632). The result is threaded into reserve_batch_payments.
    """
    base_price = price_override if price_override is not None else tier.price
    return _maybe_resolve_attendee_vat(billing_info, tier, org, base_price)


@transaction.atomic
def reserve_batch_payments(
    *,
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    tickets: list[Ticket],
    reservation_id: UUID,
    price_override: Decimal | None = None,
    billing_info: "BuyerBillingInfoSchema | None" = None,
    attendee_vat: "tuple[AttendeeVATResult | None, bool] | None" = None,
) -> None:
    """Create PENDING Payment rows for a reserved batch — NO Stripe call (#632).

    VAT (incl. any VIES round-trip) is resolved here; callers must invoke this
    BEFORE taking the TicketTier select_for_update so the row is never locked
    during VIES. The Stripe session is created later by create_batch_session,
    which stamps stripe_session_id onto these rows. Because the Payment rows
    already exist, "paid session with no Payment row" (Window B) is unreachable.
    """
    if not event.organization.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))
    base_price = price_override if price_override is not None else tier.price
    if base_price <= 0:
        raise HttpError(400, str(_("This ticket tier cannot be purchased online.")))

    org = event.organization
    # VIES runs pre-lock (Task 5) and is passed in; fall back to resolving here
    # for any caller that hasn't (keeps this helper self-contained).
    if attendee_vat is not None:
        attendee_vat_result, buyer_vat_validated = attendee_vat
    else:
        attendee_vat_result, buyer_vat_validated = _maybe_resolve_attendee_vat(billing_info, tier, org, base_price)
    effective_price = attendee_vat_result.effective_price if attendee_vat_result else base_price

    total_amount = effective_price * len(tickets)
    net_fee = (total_amount * org.platform_fee_percent / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fixed_fee = convert_currency(org.platform_fee_fixed, settings.DEFAULT_CURRENCY, tier.currency)
    net_fee_total = net_fee + fixed_fee

    site = SiteSettings.get_solo()
    total_fee_vat = calculate_platform_fee_vat(net_fee_total, org, site.platform_vat_country, site.platform_vat_rate)

    expires_at = timezone.now() + timedelta(minutes=settings.RESERVATION_HOLD_MINUTES)

    _create_payment_records(
        tickets=tickets,
        user=user,
        session_id="",
        tier=tier,
        effective_price=effective_price,
        total_fee_vat=total_fee_vat,
        attendee_vat_result=attendee_vat_result,
        billing_info=billing_info,
        buyer_vat_validated=buyer_vat_validated,
        expires_at=expires_at,
        org=org,
        reservation_id=reservation_id,
    )

    if billing_info and billing_info.save_to_profile:
        _save_billing_to_profile(user, billing_info)


@transaction.atomic
def create_batch_session(*, reservation_id: UUID) -> str:
    """Create the Stripe session for a reserved batch and stamp it (#632).

    Idempotent: reuses one Stripe session on retry/double-submit via
    idempotency_key=reservation_id. Returns the checkout URL only after the
    stamp commits, so a payable session never exists without a reconcilable
    Payment row.
    """
    payments = list(
        Payment.objects.select_related("ticket__event__organization", "ticket__tier").filter(
            reservation_id=reservation_id, status=Payment.PaymentStatus.PENDING
        )
    )
    if not payments:
        raise HttpError(404, str(_("No pending reservation found.")))
    already = [p for p in payments if p.stripe_session_id]
    if already:
        # Already sessioned: return the existing URL instead of creating a duplicate.
        return resume_pending_checkout(str(payments[0].id), payments[0].user)
    if payments[0].has_expired():
        raise HttpError(404, str(_("Reservation has expired. Please start a new purchase.")))

    tickets = [p.ticket for p in payments]
    if any(tk.held_pass_id for tk in tickets):
        # A series-pass reservation is not a valid batch reservation (#632 guard).
        # 404 (not 400) and the same message as "not found" avoids leaking reservation
        # existence/type to a client probing with someone else's reservation_id.
        raise HttpError(404, str(_("No pending reservation found.")))
    tier = tickets[0].tier
    event = tickets[0].event
    user = payments[0].user
    effective_price = payments[0].amount

    total_fee_gross = sum((p.platform_fee for p in payments), Decimal("0"))
    application_fee_amount = to_stripe_amount(total_fee_gross, tier.currency)
    site = SiteSettings.get_solo()
    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)

    session = _create_stripe_session(
        event=event,
        tier=tier,
        user=user,
        tickets=tickets,
        effective_price=effective_price,
        application_fee_amount=application_fee_amount,
        expires_at=expires_at,
        site=site,
        idempotency_key=str(reservation_id),
    )
    Payment.objects.filter(reservation_id=reservation_id, status=Payment.PaymentStatus.PENDING).update(
        stripe_session_id=session.id,
        expires_at=expires_at,
    )
    return t.cast(str, session.url)


def _create_series_pass_stripe_session(
    *,
    held_pass: HeldSeriesPass,
    org: Organization,
    user: RevelUser,
    tickets: list[Ticket],
    total: Decimal,
    application_fee_amount: int,
    expires_at: datetime,
    site: SiteSettings,
    idempotency_key: str | None = None,
) -> Session:
    """Build session data and create a Stripe Checkout Session for a series pass.

    ``idempotency_key`` (e.g. a reservation id) makes retries of an interrupted
    session-create call reuse the same Stripe session instead of double-charging
    (#632); callers with nothing to key on may omit it.

    Returns:
        The created Stripe Session object.

    Raises:
        HttpError: If Stripe API call fails.
    """
    series_pass = held_pass.series_pass
    series = series_pass.event_series
    ticket_ids = ",".join(str(ticket.id) for ticket in tickets)

    frontend_base_url = site.frontend_base_url
    series_url = f"{frontend_base_url}/events/{org.slug}/series/{series.slug}"
    session_data = dict(  # noqa: C408
        customer_email=user.email,
        line_items=[
            {
                "price_data": {
                    "currency": series_pass.currency.lower(),
                    "product_data": {
                        "name": f"Season pass: {series_pass.name} — {series.name}",
                    },
                    "unit_amount": to_stripe_amount(total, series_pass.currency),
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        success_url=f"{series_url}?payment_success=true",
        cancel_url=f"{series_url}?payment_cancelled=true",
        payment_intent_data={
            "application_fee_amount": application_fee_amount,
        },
        stripe_account=org.stripe_account_id,
        metadata={
            "held_pass_id": str(held_pass.id),
            "user_id": str(user.id),
            "ticket_ids": ticket_ids,
        },
        expires_at=int(expires_at.timestamp()),
    )

    # If the organization is using the platform's own Stripe account,
    # remove connected account parameters (no fee to ourselves)
    if settings.STRIPE_ACCOUNT == org.stripe_account_id:
        session_data.pop("stripe_account")
        session_data["payment_intent_data"].pop("application_fee_amount")  # type: ignore[union-attr, arg-type]

    try:
        return Session.create(**session_data, idempotency_key=idempotency_key)  # type: ignore[arg-type]
    except Exception as e:
        logger.error("stripe_series_pass_session_creation_failed", error=str(e), held_pass_id=str(held_pass.id))
        raise HttpError(500, str(_("Payment processing failed. Please try again later."))) from e


@transaction.atomic
def reserve_series_pass_payments(
    *,
    held_pass: HeldSeriesPass,
    tickets: list[Ticket],
    reservation_id: UUID,
    billing_info: "BuyerBillingInfoSchema | None" = None,
) -> None:
    """Create PENDING Payment rows for a reserved series pass — NO Stripe call (#632).

    N Payment rows split the pass's total price across tickets (per-ticket
    share, not per-ticket price — a series pass has a single price covering
    all its tickets). The Stripe session is created later by
    create_series_pass_session, which stamps stripe_session_id onto these rows
    and onto held_pass. Because the Payment rows already exist, "paid session
    with no Payment row" (Window B) is unreachable.

    Args:
        held_pass: The PENDING HeldSeriesPass being paid for.
        tickets: The PENDING tickets materialized for this pass.
        reservation_id: Groups these Payment rows for the follow-up session step.
        billing_info: Optional buyer billing info for attendee invoicing.

    Raises:
        HttpError: If organization not configured, or the pass price is not
            purchasable online.
    """
    series_pass = held_pass.series_pass
    org = series_pass.event_series.organization
    user = held_pass.user

    if not org.is_stripe_connected:
        raise HttpError(400, str(_("This organization is not configured to accept payments.")))

    total = held_pass.price_paid
    if total <= 0:
        raise HttpError(400, str(_("This pass cannot be purchased online.")))

    net_fee = (total * org.platform_fee_percent / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fixed_fee = convert_currency(org.platform_fee_fixed, settings.DEFAULT_CURRENCY, series_pass.currency)
    net_fee_total = net_fee + fixed_fee

    site = SiteSettings.get_solo()
    total_fee_vat = calculate_platform_fee_vat(net_fee_total, org, site.platform_vat_country, site.platform_vat_rate)

    expires_at = timezone.now() + timedelta(minutes=settings.RESERVATION_HOLD_MINUTES)

    shares = distribute_amount_across_items(total, len(tickets))
    fee_gross_shares = distribute_amount_across_items(total_fee_vat.fee_gross, len(tickets))
    fee_vat_shares = distribute_amount_across_items(total_fee_vat.fee_vat, len(tickets))

    # billing_info is out of scope for pass v1: no attendee VAT re-resolution, just a
    # snapshot for attendee invoicing.
    # ponytail: attendee reverse-charge VAT for passes deferred; upgrade = mirror
    # _maybe_resolve_attendee_vat per-tier
    billing_snapshot: BuyerBillingSnapshot | None = None
    if billing_info:
        billing_snapshot = _build_billing_snapshot(billing_info, False, False)

    tier_map = TicketTier.objects.in_bulk([ticket.tier_id for ticket in tickets])
    payments = []
    for i, ticket in enumerate(tickets):
        rate = get_effective_vat_rate(tier_map[ticket.tier_id].vat_rate, org.vat_rate)
        ticket_vat = calculate_vat_inclusive(shares[i], rate)
        payments.append(
            Payment(
                ticket=ticket,
                user=user,
                stripe_session_id="",
                reservation_id=reservation_id,
                amount=shares[i],
                platform_fee=fee_gross_shares[i],
                currency=series_pass.currency,
                status=Payment.PaymentStatus.PENDING,
                raw_response={},
                expires_at=expires_at,
                # Ticket sale VAT breakdown
                net_amount=ticket_vat.net_amount,
                vat_amount=ticket_vat.vat_amount,
                vat_rate=ticket_vat.vat_rate,
                # Platform fee VAT breakdown (distributed to avoid penny errors)
                platform_fee_net=fee_gross_shares[i] - fee_vat_shares[i],
                platform_fee_vat=fee_vat_shares[i],
                platform_fee_vat_rate=total_fee_vat.fee_vat_rate,
                platform_fee_reverse_charge=total_fee_vat.reverse_charge,
                # Buyer billing snapshot for attendee invoicing
                buyer_billing_snapshot=billing_snapshot,
            )
        )
    Payment.objects.bulk_create(payments)

    if billing_info and billing_info.save_to_profile:
        _save_billing_to_profile(user, billing_info)


@transaction.atomic
def create_series_pass_session(*, reservation_id: UUID) -> str:
    """Create the Stripe session for a reserved series pass and stamp it (#632).

    Idempotent: reuses one Stripe session on retry/double-submit via
    idempotency_key=reservation_id. Returns the checkout URL only after the
    stamp commits, so a payable session never exists without a reconcilable
    Payment row.
    """
    payments = list(
        Payment.objects.select_related(
            "ticket__held_pass__series_pass__event_series__organization", "ticket__tier"
        ).filter(reservation_id=reservation_id, status=Payment.PaymentStatus.PENDING)
    )
    if not payments:
        raise HttpError(404, str(_("No pending reservation found.")))
    already = [p for p in payments if p.stripe_session_id]
    if already:
        # Already sessioned: return the existing URL instead of creating a duplicate.
        return resume_pending_checkout(str(payments[0].id), payments[0].user)
    if payments[0].has_expired():
        raise HttpError(404, str(_("Reservation has expired. Please start a new purchase.")))

    tickets = [p.ticket for p in payments]
    if tickets[0].held_pass_id is None:
        # A batch reservation is not a valid series-pass reservation (#632 guard).
        # 404 (not 400/500) and the same message as "not found" avoids leaking
        # reservation existence/type to a client probing with someone else's id.
        raise HttpError(404, str(_("No pending reservation found.")))
    # A series-pass ticket always has held_pass set (materialize_tickets, the only
    # place tickets are created for reserve_series_pass_payments to pick up) --
    # guaranteed by the guard above.
    held_pass = t.cast(HeldSeriesPass, tickets[0].held_pass)
    series_pass = held_pass.series_pass
    org = series_pass.event_series.organization
    user = payments[0].user
    total = held_pass.price_paid

    total_fee_gross = sum((p.platform_fee for p in payments), Decimal("0"))
    application_fee_amount = to_stripe_amount(total_fee_gross, series_pass.currency)
    site = SiteSettings.get_solo()
    expires_at = timezone.now() + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)

    session = _create_series_pass_stripe_session(
        held_pass=held_pass,
        org=org,
        user=user,
        tickets=tickets,
        total=total,
        application_fee_amount=application_fee_amount,
        expires_at=expires_at,
        site=site,
        idempotency_key=str(reservation_id),
    )

    Payment.objects.filter(reservation_id=reservation_id, status=Payment.PaymentStatus.PENDING).update(
        stripe_session_id=session.id,
        expires_at=expires_at,
    )
    held_pass.stripe_session_id = session.id
    held_pass.save(update_fields=["stripe_session_id"])

    return t.cast(str, session.url)
