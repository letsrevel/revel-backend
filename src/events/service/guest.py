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
from pydantic import TypeAdapter, ValidationError

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from events import models, schema
from events.service.event_manager import EventManager

if t.TYPE_CHECKING:
    from events.service.batch_ticket_service.context import CartGroup

logger = structlog.get_logger(__name__)

# Ceiling on the guest confirmation JWT, which travels in the emailed link's query
# string. Kept well under the ~8 KB total-URL limit the strictest mainstream servers,
# mail clients and link scanners enforce, leaving room for the frontend origin, the
# path and any tracking wrapper the buyer's mail provider adds.
_GUEST_TOKEN_MAX_CHARS = 4096


def get_or_create_guest_user(email: str, first_name: str = "", last_name: str = "") -> RevelUser:
    """Get existing guest user or create a new one.

    Args:
        email: User's email address
        first_name: User's first name
        last_name: User's last name

    Returns:
        Guest user instance

    Raises:
        HttpError: If a non-guest user with this email already exists, or the email
            is globally banned.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    # Normalize email to lowercase for case-insensitive matching
    email = email.lower()

    # Reject globally-banned emails before minting a guest row — otherwise a ban could
    # be bypassed by creating a guest account (and later converting it via password reset).
    if is_email_globally_banned(email):
        logger.warning("guest_user_creation_blocked_banned_email", email=email)
        raise HttpError(403, str(BAN_ERROR_MESSAGE))

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

    # Guest user already exists — keep existing names to prevent overwrite by third parties.
    # Per-ticket guest_name is captured separately in the JWT payload.
    return existing_user


def claim_invitation_link(user: RevelUser, event: models.Event, event_token: models.EventToken | None) -> None:
    """Claim an invitation link carried by a guest request, if it is for this event.

    The guest counterpart of the join page's ``POST /events/claim-invitation/{token}``,
    which requires a login: a guest sends the same token as ``X-Event-Token`` and gets
    the same ``EventInvitation``. All token rules are ``tokens.claim_invitation``'s —
    read-only tokens, expiry and ``max_uses`` are enforced there, and it is idempotent
    for a returning guest. A token for another event is ignored. Must run AFTER the
    guest user row exists and BEFORE any eligibility or tier-access check, which is
    where the resulting invitation is consulted.

    Args:
        user: The guest user.
        event: The event being RSVP'd to or purchased for.
        event_token: The token resolved from the request, if any.
    """
    from events.service.tokens import claim_invitation

    if event_token is None or event_token.event_id != event.id:
        return
    # claim_invitation locks the token row (select_for_update) — needs a transaction.
    with transaction.atomic():
        claim_invitation(user, event_token.pk)


def create_guest_rsvp_token(
    user: RevelUser, event_id: UUID, answer: t.Literal["yes", "no", "maybe"], note: str = ""
) -> str:
    """Create JWT token for guest RSVP confirmation.

    Args:
        user: The guest user
        event_id: Event ID to RSVP to
        answer: RSVP answer
        note: Optional RSVP note to carry through to confirmation

    Returns:
        JWT token string
    """
    payload = schema.GuestRSVPJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        event_id=event_id,
        answer=answer,
        note=note,
        exp=timezone.now() + timedelta(hours=1),
        jti=str(uuid4()),
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    logger.info("guest_rsvp_token_created", user_id=str(user.id), event_id=str(event_id), answer=answer)
    return token


def create_guest_ticket_token(
    user: RevelUser,
    event_id: UUID,
    tier_id: UUID | None = None,
    tickets: list[schema.TicketPurchaseItem] | None = None,
    pwyc_amount: Decimal | None = None,
    discount_code: str | None = None,
    *,
    accessible_required: bool = False,
    price_category_id: UUID | None = None,
    guest_session: str | None = None,
    groups: "list[CartGroup] | None" = None,
) -> str:
    """Create JWT token for guest ticket purchase confirmation.

    Only used for non-online-payment tickets (free/offline/at-the-door).
    Online payment tickets go directly to Stripe without email confirmation.

    Supports two construction forms (#846), mirroring ``BatchTicketService``:

    - **Single-tier form** (legacy): pass ``tier_id``/``tickets``. The minted token
      carries the v1 flat fields at the top level — exactly what every pre-#846
      token looked like. No route mints it any more (the deprecated single-tier
      guest routes now build one-group carts); kept so tests can mint the v1 shape
      that ``confirm_guest_action`` must keep decoding for in-flight tokens.
    - **Cart form**: pass ``groups`` (one
      :class:`~events.service.batch_ticket_service.context.CartGroup` per tier).
      The token instead carries ``groups``; the flat fields stay at their defaults.

    Exactly one of ``tier_id`` / ``groups`` must be given.

    Args:
        user: The guest user.
        event_id: Event ID.
        tier_id: Ticket tier ID — single-tier form only.
        tickets: List of ticket purchase items with guest_name and optional seat_id —
            single-tier form only.
        pwyc_amount: Optional PWYC amount — single-tier form only (the cart form
            carries it per group, on ``CartGroup.pwyc_amount``).
        discount_code: Optional discount code string. Cart-level either way — a
            multi-tier cart's code is not per group, matching
            ``MultiTierCheckoutPayload``/``GuestMultiTierCheckoutPayload``.
        accessible_required: Single-tier form only (per group in the cart form).
        price_category_id: Single-tier form only (per group in the cart form).
        guest_session: Hold-owner session id captured at checkout, embedded in the
            token so confirm-time assignment consumes the buyer's own holds even
            when the confirmation link is opened on a different device.
        groups: The cart's groups — cart form only.

    Returns:
        JWT token string.

    Raises:
        TypeError: If neither, or both, of ``tier_id``/``groups`` are given, or the
            single-tier form is missing ``tickets``.
    """
    if (tier_id is None) == (not groups):
        raise TypeError("exactly one of tier_id or groups is required")

    if groups:
        group_payloads = [
            schema.GuestCheckoutGroupPayload(
                tier_id=g.tier.id,
                # None ("no name given") travels as "" in the token; confirm maps it back.
                tickets=[
                    schema.GuestTicketItemPayload(guest_name=item.guest_name or "", seat_id=item.seat_id)
                    for item in g.items
                ],
                pwyc_amount=g.pwyc_amount,
                price_category_id=g.price_category_id,
                accessible_required=g.accessible_required,
            )
            for g in groups
        ]
        payload = schema.GuestTicketJWTPayloadSchema(
            user_id=user.id,
            email=user.email,
            event_id=event_id,
            discount_code=discount_code,
            guest_session=guest_session,
            groups=group_payloads,
            exp=timezone.now() + timedelta(hours=1),
            jti=str(uuid4()),
        )
        log_tier_ids = [str(g.tier.id) for g in groups]
        log_ticket_count = sum(len(g.items) for g in groups)
    else:
        if tickets is None:
            raise TypeError("tickets is required in the single-tier form")
        # Convert TicketPurchaseItem to GuestTicketItemPayload for JWT storage
        # None ("no name given") travels as "" in the token; confirm maps it back.
        ticket_payloads = [
            schema.GuestTicketItemPayload(guest_name=item.guest_name or "", seat_id=item.seat_id) for item in tickets
        ]
        payload = schema.GuestTicketJWTPayloadSchema(
            user_id=user.id,
            email=user.email,
            event_id=event_id,
            tier_id=tier_id,
            pwyc_amount=pwyc_amount,
            discount_code=discount_code,
            tickets=ticket_payloads,
            accessible_required=accessible_required,
            price_category_id=price_category_id,
            guest_session=guest_session,
            exp=timezone.now() + timedelta(hours=1),
            jti=str(uuid4()),
        )
        log_tier_ids = [str(tier_id)]
        log_ticket_count = len(tickets)

    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    logger.info(
        "guest_ticket_token_created",
        user_id=str(user.id),
        event_id=str(event_id),
        tier_ids=log_tier_ids,
        ticket_count=log_ticket_count,
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
    except jwt.PyJWTError:
        logger.warning("guest_token_validation_failed")
        raise HttpError(400, str(_("Invalid token.")))

    # Validate with discriminated union
    try:
        adapter: TypeAdapter[schema.GuestActionPayload] = TypeAdapter(schema.GuestActionPayload)
        payload: schema.GuestActionPayload = adapter.validate_python(raw_payload)
    except ValidationError as e:
        logger.warning("guest_payload_validation_failed", error=str(e))
        raise HttpError(400, str(_("Invalid token payload.")))

    check_blacklist(payload.jti)
    logger.info("guest_token_validated", user_id=str(payload.user_id), event_id=str(payload.event_id))
    return payload


@transaction.atomic
def handle_guest_rsvp(
    event: models.Event,
    answer: models.EventRSVP.RsvpStatus,
    email: str,
    first_name: str,
    last_name: str,
    note: str = "",
    event_token: models.EventToken | None = None,
) -> schema.GuestActionResponseSchema:
    """Handle guest RSVP request (business logic extracted from controller).

    Args:
        event: Event object
        answer: RSVP answer
        email: Guest email
        first_name: Guest first name
        last_name: Guest last name
        note: Optional RSVP note (rejected if the event doesn't accept notes)
        event_token: Invitation link carried by the request, claimed for the guest
            before eligibility is checked (see :func:`claim_invitation_link`)

    Returns:
        Response with confirmation message

    Raises:
        HttpError: If event doesn't allow guest access, doesn't accept notes but one was
            provided, or eligibility checks fail
    """
    from events.tasks import send_guest_rsvp_confirmation

    # Check if event allows guest access
    if not event.can_attend_without_login:
        raise HttpError(400, str(_("This event requires login to RSVP.")))

    if note and not event.accept_rsvp_notes:
        raise HttpError(400, str(_("This event does not accept RSVP notes.")))

    # Create or update guest user
    user = get_or_create_guest_user(email, first_name, last_name)
    claim_invitation_link(user, event, event_token)

    # Check eligibility (without creating RSVP yet)
    manager = EventManager(user, event)
    manager.check_eligibility(raise_on_false=True)

    # Create JWT token for confirmation (convert Status enum to string literal)
    answer_str = t.cast(t.Literal["yes", "no", "maybe"], answer.value)
    token = create_guest_rsvp_token(user, event.id, answer_str, note=note)

    # Send confirmation email
    transaction.on_commit(lambda: send_guest_rsvp_confirmation.delay(user.email, token, event.name))

    return schema.GuestActionResponseSchema(message=str(_("Please check your email to confirm your RSVP")))


def _cart_tier_name_summary(groups: "list[CartGroup]", max_length: int = 120) -> str:
    """Build the confirmation email's ``tier_name`` context var for a guest cart (#846).

    A single-group cart keeps the exact string the template has always shown (just
    that one tier's name). A multi-group cart lists every tier name, comma-joined and
    capped — the template only needs a human-readable label for the sentence "You've
    requested a ticket for {event} ({tier_name})", not a full itemization.

    Args:
        groups: The cart's groups, in cart order.
        max_length: Truncation ceiling so a 20-tier cart can't blow up the subject/body.

    Returns:
        The tier-name label for the email.
    """
    if len(groups) == 1:
        return groups[0].tier.name
    names = ", ".join(group.tier.name for group in groups)
    if len(names) > max_length:
        names = names[: max_length - 1].rstrip() + "…"
    return names


def handle_guest_ticket_checkout(
    event: models.Event,
    groups: "list[CartGroup]",
    email: str,
    first_name: str,
    last_name: str,
    *,
    discount_code: str | None = None,
    billing_info: "schema.BuyerBillingInfoSchema | None" = None,
    guest_session: str | None = None,
    event_token: models.EventToken | None = None,
) -> schema.GuestCheckoutResponseSchema:
    """Handle guest ticket checkout request, spanning as many tiers as the cart holds (#846).

    Ordering is load-bearing (#846 review fix): the guest-access gate must run
    BEFORE any guest ``RevelUser`` row is created and before the discount code is
    validated — running either first would (a) create a guest account for an
    attacker-supplied email even when the event requires login, and (b) turn "an
    account with this email already exists" into an account-existence oracle
    answered *before* the login-required 400. The eligibility check runs right
    after user creation (``EventManager`` needs a real user) and before the
    discount/PWYC validation. This function alone owns that order — callers pass
    a raw discount code string, never a pre-validated ``DiscountCode``.

    Args:
        event: Event object.
        groups: One :class:`~events.service.batch_ticket_service.context.CartGroup`
            per tier in the cart. Tiers are resolved by the caller; validation
            (cart shape, PWYC, zone, discount, eligibility) all happens here.
        email: Guest email.
        first_name: Guest first name.
        last_name: Guest last name.
        discount_code: Optional discount code string. Validated here (cart-aware,
            via ``discount_code_service.validate_cart_discount`` — the same helper
            the authenticated multi-tier endpoint uses), after eligibility.
        billing_info: Optional buyer billing info for attendee invoicing.
        guest_session: Resolved guest-hold session id (seat holds are owned by it).
        event_token: Invitation link carried by the request, claimed for the guest
            before eligibility and tier access are checked (see :func:`claim_invitation_link`).

    Returns:
        GuestCheckoutResponseSchema. Non-online carts: `message` (email confirmation
        sent). Online carts: `requires_payment=True` and a `reservation_id` (#632) —
        the caller must then POST the guest `checkout-session` endpoint to obtain the
        Stripe `checkout_url`.

    Raises:
        HttpError: If event doesn't allow guest access, the cart is malformed, tier
            issues, eligibility checks fail, or (non-online carts) the confirmation
            token would exceed ``_GUEST_TOKEN_MAX_CHARS``.
        InvalidZoneSelectionError: 400 if a requested zone is unusable on its tier.
    """
    from events.service import discount_code_service
    from events.service.batch_ticket_service import BatchTicketService
    from events.service.batch_ticket_service.context import validate_cart_shape
    from events.service.batch_ticket_service.eligibility import assert_cart_purchasable_by, assert_sale_window
    from events.service.seating.pick import resolve_requested_zone
    from events.tasks import send_guest_ticket_confirmation

    # Check if event allows guest access
    if not event.can_attend_without_login:
        raise HttpError(400, str(_("This event requires login to purchase tickets.")))

    # Enforce the holder-name requirement HERE, before the payment-method branch: the
    # non-online branch defers create_batch to the confirmation click, so a nameless
    # cart would otherwise cost the buyer an email and a dead link instead of a 400.
    # create_batch stays the authoritative gate on every path that reaches it.
    all_items = [item for group in groups for item in group.items]
    if event.require_ticket_names and any(item.guest_name is None for item in all_items):
        raise HttpError(400, str(_("This event requires a name on every ticket.")))

    # Enforce the per-tier sale window HERE, before the payment-method branch: the
    # any-tier TicketSalesGate below passes as long as *some* tier is on sale, and the
    # non-online branch defers create_batch — the only other place the window is
    # checked — to the confirmation click, so a closed-window group would otherwise
    # cost the buyer an email and a dead link instead of a 403.
    # Residual: the window can still close between this check and the click (the token
    # lives an hour), and create_batch will then reject the confirmation.
    for group in groups:
        assert_sale_window(group.tier)

    # Run the VIES round-trip HERE, before any row lock is taken: claim_invitation_link
    # below locks the EventToken row for the rest of the request (ATOMIC_REQUESTS — the
    # inner atomic() only releases a savepoint), and create_batch takes the tier locks.
    # Neither may be held across a network call (#632). Answerable with no user at all,
    # so it sits with the other user-independent gates. Only the paid-online path needs
    # it; create_batch resolves it itself when handed None.
    from events.service import stripe_service

    buyer_vat_context = None
    if groups[0].tier.payment_method == models.TicketTier.PaymentMethod.ONLINE and billing_info:
        buyer_vat_context = stripe_service.resolve_attendee_vat_for_reserve(billing_info=billing_info)

    # Create or update guest user. Must come AFTER the gates above (#846 review
    # fix): all are answerable with no user at all, and creating a row / touching
    # the "does an account exist" check before them turns a login-required event
    # into an account-creation side channel / existence oracle.
    user = get_or_create_guest_user(email, first_name, last_name)
    claim_invitation_link(user, event, event_token)

    # Check eligibility (before validating PWYC/discount to prevent information leakage)
    manager = EventManager(user, event)
    manager.check_eligibility(raise_on_false=True)

    # Enforce each tier's purchasable_by rule HERE, before the payment-method branch,
    # for the same reason as the sale window above: the non-online branch defers
    # create_batch — the only other place the rule runs — to the confirmation click,
    # so an uninvited guest on an invited-only tier would otherwise get a 200, an
    # email, and a link that 403s. Must run AFTER get_or_create_guest_user: a pending
    # email invitation becomes this user's EventInvitation on row creation.
    assert_cart_purchasable_by(event, user, (group.tier for group in groups))

    # Cart-shape validation (duplicate tier, uniform currency/payment method, PWYC
    # required-iff-PWYC and in bounds, no seat twice) — the single authority shared
    # with BatchTicketService (#846 review fix; see validate_cart_shape's docstring).
    # Checked here too, not only inside create_batch: the non-online branch below
    # never reaches create_batch until the confirmation click, so a malformed cart
    # must 400 now — not after an email already promised a purchase — and this is
    # also what proves every group agrees on the payment method the branch below reads.
    validate_cart_shape(groups)

    # Validate the discount code if provided — AFTER eligibility, mirroring the
    # pre-#846 single-tier handler's order. Cart-aware even for a one-group cart,
    # via the same helper backing the authenticated multi-tier endpoint (#846): a
    # code need not apply to every group, only some.
    dc, valid_tier_ids = None, None
    if discount_code:
        dc, valid_tier_ids = discount_code_service.validate_cart_discount(discount_code, event, groups, user)

    # Validate the requested zone per group HERE, not only downstream: the
    # non-online branch below defers seat assignment to the confirmation click, so
    # an unusable zone would otherwise cost the buyer an email and a dead link
    # instead of a 400.
    for group in groups:
        resolve_requested_zone(group.tier, group.price_category_id)

    payment_method = groups[0].tier.payment_method  # uniform — validate_cart_shape just proved it

    # Branch by payment method
    if payment_method == models.TicketTier.PaymentMethod.ONLINE:
        # Online payment: use BatchTicketService (Stripe provides security)
        service = BatchTicketService(
            event,
            user=user,
            groups=groups,
            discount_code=dc,
            discount_valid_tier_ids=valid_tier_ids,
            guest_session=guest_session,
        )
        result = service.create_batch(billing_info=billing_info, buyer_vat_context=buyer_vat_context)

        # Branch on the returned SHAPE, never on the tier's payment method (#740):
        # a PWYC/discount input that zeroes every unit reroutes an ONLINE cart to
        # the free checkout, which returns a bare list of ACTIVE tickets.
        # ponytail: create_batch's dual return type is what invites this at every
        # call site; a single result object carrying an optional reservation_id
        # would make it unrepresentable (~104 call sites, mostly tests — see #740).
        if isinstance(result, tuple):
            _tickets, reservation_id = result
            return schema.GuestCheckoutResponseSchema(
                message=None,
                checkout_url=None,
                tickets=[],
                reservation_id=reservation_id,
                requires_payment=True,
            )

        return schema.GuestCheckoutResponseSchema(
            message=None,
            checkout_url=None,
            tickets=[schema.UserTicketSchema.from_orm(ticket) for ticket in result],
            requires_payment=False,
        )
    else:
        # Non-online payment: require email confirmation
        # Store the cart in the JWT token for later creation
        token = create_guest_ticket_token(
            user,
            event.id,
            groups=groups,
            discount_code=discount_code,
            guest_session=guest_session,
        )
        # A multi-tier cart is serialized WHOLE into this token (every group, every
        # item's guest_name and seat_id), so a wide cart with long holder names mints
        # a JWT of tens of KB. The confirmation link carries it as a query parameter,
        # and mail clients, link scanners and proxies truncate long URLs — the buyer
        # would get a 200 here and a dead link in their inbox, with no ticket and no
        # way back. Refuse up front instead. The ceiling is the token alone; the rest
        # of the URL (frontend origin + path) is comfortably inside the ~8 KB that the
        # most restrictive mainstream handlers accept.
        # ponytail: the real fix is to persist the cart and put an opaque handle in the
        # link (as the ONLINE branch's reservation_id already does) — see #632.
        if len(token) > _GUEST_TOKEN_MAX_CHARS:
            raise HttpError(
                400, str(_("Your cart is too large for guest checkout. Please log in or split your purchase."))
            )

        tier_name = _cart_tier_name_summary(groups)
        transaction.on_commit(lambda: send_guest_ticket_confirmation.delay(user.email, token, event.name, tier_name))
        return schema.GuestCheckoutResponseSchema(
            message=str(_("Please check your email to confirm your ticket purchase")),
            checkout_url=None,
            tickets=[],
        )


@transaction.atomic
def confirm_guest_action(
    token: str, guest_session: str | None = None
) -> schema.EventRSVPSchema | schema.BatchCheckoutResponse:
    """Confirm a guest action (RSVP or ticket purchase) via JWT token.

    Uses Pydantic's discriminated union to properly decode the token type.

    Args:
        token: JWT token string
        guest_session: Resolved guest-hold session id of the confirming browser,
            so the guest's own seat holds are consumed rather than blocking them

    Returns:
        Created RSVP or BatchCheckoutResponse with ticket(s)

    Raises:
        HttpError: If token is invalid, expired, already used, or eligibility checks fail
    """
    from events.service.batch_ticket_service import BatchTicketService, CartGroup

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

        # Drop the note (never fail the confirmation) if the organizer
        # disabled notes between email-send and link-click.
        note = payload.note if event.accept_rsvp_notes else ""
        rsvp = manager.rsvp(answer_enum, note=note)

        # Blacklist token
        blacklist_token(token)

        return schema.EventRSVPSchema.from_orm(rsvp)

    elif isinstance(payload, schema.GuestTicketJWTPayloadSchema):
        # Handle ticket confirmation
        from events.service import discount_code_service

        event = get_object_or_404(models.Event, id=payload.event_id)

        # Normalize both token generations into one shape (#846): a v2 (grouped)
        # token carries `groups` directly; a v1 (flat) token — every one minted
        # before this deploy, plus one built by create_guest_ticket_token's
        # single-tier form — synthesizes a single group from its top-level fields,
        # preserving the legacy "no tickets list" fallback to the user's own name.
        if payload.groups:
            raw_groups = payload.groups
        else:
            if payload.tier_id is None:
                # Every legacy (v1) token was minted with a tier_id; a payload with
                # neither `groups` nor `tier_id` is not a shape this schema allows to
                # exist, but mypy can't see that across the discriminated union.
                raise HttpError(400, str(_("Invalid token payload.")))
            raw_groups = [
                schema.GuestCheckoutGroupPayload(
                    tier_id=payload.tier_id,
                    tickets=payload.tickets or [schema.GuestTicketItemPayload(guest_name=user.get_display_name())],
                    pwyc_amount=payload.pwyc_amount,
                    price_category_id=payload.price_category_id,
                    accessible_required=payload.accessible_required,
                )
            ]

        tiers = {
            raw_group.tier_id: get_object_or_404(models.TicketTier, id=raw_group.tier_id, event=event)
            for raw_group in raw_groups
        }

        # Re-check eligibility (event state may have changed)
        manager = EventManager(user, event)
        manager.check_eligibility(raise_on_false=True)

        # Convert JWT payload items back to TicketPurchaseItem for BatchTicketService,
        # once per group — shared by the discount re-check and the cart build below.
        items_by_tier = {
            raw_group.tier_id: [
                schema.TicketPurchaseItem(guest_name=item.guest_name or None, seat_id=item.seat_id)
                for item in raw_group.tickets
            ]
            for raw_group in raw_groups
        }

        groups = [
            CartGroup(
                tier=tiers[raw_group.tier_id],
                items=items_by_tier[raw_group.tier_id],
                pwyc_amount=raw_group.pwyc_amount,
                price_category_id=raw_group.price_category_id,
                accessible_required=raw_group.accessible_required,
            )
            for raw_group in raw_groups
        ]

        # Re-validate the discount code if one was stored in the token, exactly like
        # the multi-tier checkout endpoint (#846): only the groups it actually
        # applies to get it, so a code scoped to one tier cannot leak a discount
        # onto the rest of a multi-tier cart.
        dc, valid_tier_ids = None, None
        if payload.discount_code:
            dc, valid_tier_ids = discount_code_service.validate_cart_discount(
                payload.discount_code, event, groups, user
            )

        # Use BatchTicketService for proper seat handling
        service = BatchTicketService(
            event,
            user=user,
            groups=groups,
            discount_code=dc,
            discount_valid_tier_ids=valid_tier_ids,
            # Prefer the hold-owner session captured in the token so the buyer's own
            # holds are consumed even when confirming from a different device; fall
            # back to the confirming request's cookie for legacy tokens (None).
            guest_session=payload.guest_session or guest_session,
        )
        result = service.create_batch()

        # Blacklist token after successful creation
        blacklist_token(token)

        # Branch on the returned SHAPE, as at the first call site (#740). The token
        # is only minted for non-online tiers, but the tier can be flipped to ONLINE
        # between the email being sent and the buyer clicking it — then create_batch
        # reserves and returns (tickets, reservation_id), and the buyer must get the
        # reservation handle rather than a 500 for work already committed.
        if isinstance(result, tuple):
            _tickets, reservation_id = result
            return schema.BatchCheckoutResponse(
                checkout_url=None,
                tickets=[],
                reservation_id=reservation_id,
                requires_payment=True,
            )

        return schema.BatchCheckoutResponse(
            checkout_url=None,
            tickets=[schema.UserTicketSchema.from_orm(t) for t in result],
        )

    # This should never happen with proper discriminated union, but satisfy mypy
    raise HttpError(400, str(_("Invalid token type")))
