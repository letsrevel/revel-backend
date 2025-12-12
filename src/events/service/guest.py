"""Guest user service layer for events."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import jwt
import structlog
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from pydantic import TypeAdapter

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from events import models, schema
from events.service.event_manager import EventManager

logger = structlog.get_logger(__name__)


def get_or_create_guest_user(email: str, first_name: str = "", last_name: str = "") -> RevelUser:
    """Get existing guest user or create a new one.

    Args:
        email: User's email address
        first_name: User's first name
        last_name: User's last name

    Returns:
        Guest user instance

    Raises:
        HttpError: If a non-guest user with this email already exists
    """
    # Normalize email to lowercase for case-insensitive matching
    email = email.lower()

    # Check if user exists (case-insensitive)
    existing_user = RevelUser.objects.filter(email__iexact=email).first()

    if existing_user is None:
        user = RevelUser.objects.create(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
            guest=True,
            email_verified=False,
            is_active=True,  # Guest users need to be active to access their tickets/RSVPs
            password=make_password(None),  # Unusable password
        )
        logger.info("guest_user_created", email=email, user_id=str(user.id))
        return user

    if not existing_user.guest:
        # Non-guest user exists, reject
        logger.warning("guest_user_creation_blocked_existing_account", email=email)
        raise HttpError(400, str(_("An account with this email already exists. Please log in.")))

    # Guest user exists, update name if provided
    if existing_user.first_name != first_name or existing_user.last_name != last_name:
        existing_user.first_name = first_name
        existing_user.last_name = last_name
        existing_user.save(update_fields=["first_name", "last_name"])
        logger.info("guest_user_updated", email=email, user_id=str(existing_user.id))

    return existing_user


def create_guest_rsvp_token(user: RevelUser, event_id: UUID, answer: t.Literal["yes", "no", "maybe"]) -> str:
    """Create JWT token for guest RSVP confirmation.

    Args:
        user: The guest user
        event_id: Event ID to RSVP to
        answer: RSVP answer

    Returns:
        JWT token string
    """
    payload = schema.GuestRSVPJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        event_id=event_id,
        answer=answer,
        exp=timezone.now() + timedelta(hours=1),
        jti=str(uuid4()),
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    logger.info("guest_rsvp_token_created", user_id=str(user.id), event_id=str(event_id), answer=answer)
    return token


def create_guest_ticket_token(
    user: RevelUser,
    event_id: UUID,
    tier_id: UUID,
    tickets: list[schema.TicketPurchaseItem],
    pwyc_amount: Decimal | None = None,
) -> str:
    """Create JWT token for guest ticket purchase confirmation.

    Only used for non-online-payment tickets (free/offline/at-the-door).
    Online payment tickets go directly to Stripe without email confirmation.

    Args:
        user: The guest user
        event_id: Event ID
        tier_id: Ticket tier ID
        tickets: List of ticket purchase items with guest_name and optional seat_id
        pwyc_amount: Optional PWYC amount

    Returns:
        JWT token string
    """
    # Convert TicketPurchaseItem to GuestTicketItemPayload for JWT storage
    ticket_payloads = [schema.GuestTicketItemPayload(guest_name=t.guest_name, seat_id=t.seat_id) for t in tickets]

    payload = schema.GuestTicketJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        event_id=event_id,
        tier_id=tier_id,
        pwyc_amount=pwyc_amount,
        tickets=ticket_payloads,
        exp=timezone.now() + timedelta(hours=1),
        jti=str(uuid4()),
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    logger.info(
        "guest_ticket_token_created",
        user_id=str(user.id),
        event_id=str(event_id),
        tier_id=str(tier_id),
        ticket_count=len(tickets),
        pwyc_amount=str(pwyc_amount) if pwyc_amount else None,
    )
    return token


def validate_and_decode_guest_token(token: str) -> schema.GuestActionPayload:
    """Validate and decode guest action JWT token using discriminated union.

    Args:
        token: JWT token string

    Returns:
        Validated payload (either GuestRSVPJWTPayloadSchema or GuestTicketJWTPayloadSchema)

    Raises:
        HttpError: If token is invalid, expired, or blacklisted
    """
    # Decode JWT manually
    try:
        raw_payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
    except jwt.ExpiredSignatureError:
        logger.warning("guest_token_validation_expired")
        raise HttpError(400, str(_("Token has expired.")))
    except Exception as e:
        logger.warning("guest_token_validation_failed", error=str(e))
        raise HttpError(400, str(_("Invalid token: {error}")).format(error=e))

    # Validate with discriminated union
    try:
        adapter: TypeAdapter[schema.GuestActionPayload] = TypeAdapter(schema.GuestActionPayload)
        payload: schema.GuestActionPayload = adapter.validate_python(raw_payload)
    except Exception as e:
        logger.warning("guest_payload_validation_failed", error=str(e))
        raise HttpError(400, str(_("Invalid token payload: {error}")).format(error=e))

    check_blacklist(payload.jti)
    logger.info("guest_token_validated", user_id=str(payload.user_id), event_id=str(payload.event_id))
    return payload


def handle_guest_rsvp(
    event: models.Event, answer: models.EventRSVP.RsvpStatus, email: str, first_name: str, last_name: str
) -> schema.GuestActionResponseSchema:
    """Handle guest RSVP request (business logic extracted from controller).

    Args:
        event: Event object
        answer: RSVP answer
        email: Guest email
        first_name: Guest first name
        last_name: Guest last name

    Returns:
        Response with confirmation message

    Raises:
        HttpError: If event doesn't allow guest access or eligibility checks fail
    """
    from events.tasks import send_guest_rsvp_confirmation

    # Check if event allows guest access
    if not event.can_attend_without_login:
        raise HttpError(400, str(_("This event requires login to RSVP.")))

    # Create or update guest user
    user = get_or_create_guest_user(email, first_name, last_name)

    # Check eligibility (without creating RSVP yet)
    manager = EventManager(user, event)
    manager.check_eligibility(raise_on_false=True)

    # Create JWT token for confirmation (convert Status enum to string literal)
    answer_str = t.cast(t.Literal["yes", "no", "maybe"], answer.value)
    token = create_guest_rsvp_token(user, event.id, answer_str)

    # Send confirmation email
    send_guest_rsvp_confirmation.delay(user.email, token, event.name)

    return schema.GuestActionResponseSchema(message=str(_("Please check your email to confirm your RSVP")))


def handle_guest_ticket_checkout(
    event: models.Event,
    tier: models.TicketTier,
    email: str,
    first_name: str,
    last_name: str,
    tickets: list[schema.TicketPurchaseItem],
    pwyc_amount: Decimal | None = None,
) -> schema.GuestCheckoutResponseSchema:
    """Handle guest ticket checkout request (business logic extracted from controller).

    Args:
        event: Event object
        tier: Ticket tier object
        email: Guest email
        first_name: Guest first name
        last_name: Guest last name
        tickets: List of ticket purchase items with guest_name and optional seat_id
        pwyc_amount: Optional PWYC amount (must be the same for all tickets)

    Returns:
        GuestCheckoutResponseSchema with either message (non-online) or checkout_url (online)

    Raises:
        HttpError: If event doesn't allow guest access, tier issues, or eligibility checks fail
    """
    from events.service.batch_ticket_service import BatchTicketService
    from events.tasks import send_guest_ticket_confirmation

    # Check if event allows guest access
    if not event.can_attend_without_login:
        raise HttpError(400, str(_("This event requires login to purchase tickets.")))

    # Create or update guest user
    user = get_or_create_guest_user(email, first_name, last_name)

    # Check eligibility (before validating PWYC to prevent information leakage)
    manager = EventManager(user, event)
    manager.check_eligibility(raise_on_false=True)

    # Validate PWYC amount if provided (after eligibility confirmed)
    if pwyc_amount is not None:
        if pwyc_amount < tier.pwyc_min:
            raise HttpError(400, str(_("PWYC amount must be at least {min_amount}")).format(min_amount=tier.pwyc_min))

        if tier.pwyc_max and pwyc_amount > tier.pwyc_max:
            raise HttpError(400, str(_("PWYC amount must be at most {max_amount}")).format(max_amount=tier.pwyc_max))

    # Branch by payment method
    if tier.payment_method == models.TicketTier.PaymentMethod.ONLINE:
        # Online payment: use BatchTicketService (Stripe provides security)
        service = BatchTicketService(event, tier, user)
        result = service.create_batch(tickets, price_override=pwyc_amount)

        if isinstance(result, str):
            return schema.GuestCheckoutResponseSchema(message=None, checkout_url=result, tickets=[])
        # This shouldn't happen for ONLINE payment, but handle it
        return schema.GuestCheckoutResponseSchema(
            message=None,
            checkout_url=None,
            tickets=[schema.UserTicketSchema.from_orm(t) for t in result],
        )
    else:
        # Non-online payment: require email confirmation
        # Store ticket info in JWT token for later creation
        token = create_guest_ticket_token(user, event.id, tier.id, tickets, pwyc_amount)
        send_guest_ticket_confirmation.delay(user.email, token, event.name, tier.name)
        return schema.GuestCheckoutResponseSchema(
            message=str(_("Please check your email to confirm your ticket purchase")),
            checkout_url=None,
            tickets=[],
        )


@transaction.atomic
def confirm_guest_action(token: str) -> schema.EventRSVPSchema | schema.BatchCheckoutResponse:
    """Confirm a guest action (RSVP or ticket purchase) via JWT token.

    Uses Pydantic's discriminated union to properly decode the token type.

    Args:
        token: JWT token string

    Returns:
        Created RSVP or BatchCheckoutResponse with ticket(s)

    Raises:
        HttpError: If token is invalid, expired, already used, or eligibility checks fail
    """
    from events.service.batch_ticket_service import BatchTicketService

    # Decode token using discriminated union
    payload = validate_and_decode_guest_token(token)

    # Get user
    user = get_object_or_404(RevelUser, id=payload.user_id)

    if isinstance(payload, schema.GuestRSVPJWTPayloadSchema):
        # Handle RSVP confirmation
        event = get_object_or_404(models.Event, id=payload.event_id)

        # Re-check eligibility (event state may have changed)
        manager = EventManager(user, event)

        # Convert string literal back to Status enum
        answer_enum = models.EventRSVP.RsvpStatus(payload.answer)
        rsvp = manager.rsvp(answer_enum)

        # Blacklist token
        blacklist_token(token)

        return schema.EventRSVPSchema.from_orm(rsvp)

    elif isinstance(payload, schema.GuestTicketJWTPayloadSchema):
        # Handle ticket confirmation
        event = get_object_or_404(models.Event, id=payload.event_id)
        tier = get_object_or_404(models.TicketTier, id=payload.tier_id, event=event)

        # Re-check eligibility (event state may have changed)
        manager = EventManager(user, event)
        manager.check_eligibility(raise_on_false=True)

        # Convert JWT payload items back to TicketPurchaseItem for BatchTicketService
        # Handle legacy tokens that don't have tickets list (backward compatibility)
        if payload.tickets:
            ticket_items = [
                schema.TicketPurchaseItem(guest_name=t.guest_name, seat_id=t.seat_id) for t in payload.tickets
            ]
        else:
            # Legacy token without tickets list - create single ticket with user's name
            ticket_items = [schema.TicketPurchaseItem(guest_name=user.get_display_name())]

        # Use BatchTicketService for proper seat handling
        service = BatchTicketService(event, tier, user)
        result = service.create_batch(ticket_items, price_override=payload.pwyc_amount)

        # Blacklist token after successful creation
        blacklist_token(token)

        # Should always return tickets for non-online payment (what email confirmation is used for)
        if isinstance(result, list):
            # Always return BatchCheckoutResponse for consistency
            return schema.BatchCheckoutResponse(
                checkout_url=None,
                tickets=[schema.UserTicketSchema.from_orm(t) for t in result],
            )

        raise HttpError(500, str(_("Unexpected response from ticket creation")))

    # This should never happen with proper discriminated union, but satisfy mypy
    raise HttpError(400, str(_("Invalid token type")))
