from uuid import UUID

from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import (
    api_controller,
    route,
)

from common.authentication import OptionalAuth
from common.schema import ResponseMessage
from common.throttling import WriteThrottle
from events import models, schema
from events.service import guest as guest_service
from events.service.guest_hold_session import GUEST_HOLD_COOKIE, resolve_guest_session

from .base import EventPublicBaseController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventPublicGuestController(EventPublicBaseController):
    """Handles guest user (unauthenticated) checkout and RSVP operations."""

    def _resolve_guest_session(self) -> str | None:
        """Resolve the guest-hold cookie, if present and valid (seat holds are owned by it)."""
        return resolve_guest_session(self.context.request.COOKIES.get(GUEST_HOLD_COOKIE))  # type: ignore[union-attr]

    @route.post(
        "/{uuid:event_id}/rsvp/{answer}/public",
        url_name="guest_rsvp",
        response={200: schema.GuestActionResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_rsvp(
        self, event_id: UUID, answer: models.EventRSVP.RsvpStatus, payload: schema.GuestRSVPRequestSchema
    ) -> schema.GuestActionResponseSchema:
        """RSVP to an event without authentication (guest user).

        Creates or updates a guest user and sends a confirmation email. The RSVP is created only
        after the user confirms via the email link. Requires event.can_attend_without_login=True.
        Returns 400 if event doesn't allow guest access or if a non-guest account exists with
        the provided email. Accepts an optional plain-text ``note`` (max 500 chars) when the event
        has ``accept_rsvp_notes`` enabled; the note is applied when the RSVP is confirmed via the
        email link.
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        return guest_service.handle_guest_rsvp(
            event, answer, payload.email, payload.first_name, payload.last_name, note=payload.note
        )

    @route.post(
        "/{uuid:event_id}/tickets/{tier_id}/checkout/public",
        url_name="guest_ticket_checkout",
        response={200: schema.GuestCheckoutResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_ticket_checkout(
        self, event_id: UUID, tier_id: UUID, payload: schema.GuestBatchCheckoutPayload
    ) -> schema.GuestCheckoutResponseSchema:
        """Purchase fixed-price tickets without authentication (guest user).

        Supports batch purchases with individual guest names per ticket. For online payment: creates
        guest user and returns Stripe checkout URL immediately (no email confirmation). For
        free/offline/at-the-door tickets: sends confirmation email first. Requires
        event.can_attend_without_login=True. Returns 400 if event doesn't allow guest access, if a
        non-guest account exists with the email, or for PWYC tiers (use /pwyc endpoint instead).

        **Request Body:**
        - `email`: Guest user's email address
        - `first_name`: Guest user's first name
        - `last_name`: Guest user's last name
        - `tickets`: List of tickets to purchase, each with:
          - `guest_name`: Name of the ticket holder (required)
          - `seat_id`: Seat UUID for USER_CHOICE seat assignment mode (optional)

        **Seat Assignment Modes:**
        - `NONE`: No seat assigned (general admission)
        - `BEST_AVAILABLE`: System auto-assigns the best adjacent block of seats
        - `USER_CHOICE`: User must provide seat_id for each ticket

        **Online tiers:** returns `requires_payment=true` and a `reservation_id`. Call
        `POST /events/reservations/{reservation_id}/checkout-session/public` next to
        obtain the Stripe `checkout_url`. Free / offline / at-the-door tiers complete
        here (`requires_payment=false`, email confirmation sent).
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.maybe_user()),
            pk=tier_id,
            event=event,
        )
        if tier.price_type == models.TicketTier.PriceType.PWYC:
            raise HttpError(400, str(_("Use /pwyc endpoint for pay-what-you-can tickets")))
        return guest_service.handle_guest_ticket_checkout(
            event,
            tier,
            payload.email,
            payload.first_name,
            payload.last_name,
            payload.tickets,
            discount_code=payload.discount_code,
            billing_info=payload.billing_info,
            guest_session=self._resolve_guest_session(),
        )

    @route.post(
        "/{uuid:event_id}/tickets/{tier_id}/checkout/pwyc/public",
        url_name="guest_ticket_pwyc_checkout",
        response={200: schema.GuestCheckoutResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_ticket_pwyc_checkout(
        self, event_id: UUID, tier_id: UUID, payload: schema.GuestBatchCheckoutPWYCPayload
    ) -> schema.GuestCheckoutResponseSchema:
        """Purchase PWYC tickets without authentication (guest user).

        Supports batch purchases with individual guest names per ticket. All tickets in the batch are
        purchased at the same price_per_ticket amount. For online payment: creates guest user and
        returns Stripe checkout URL immediately. For free/offline/at-the-door tickets: sends
        confirmation email first. Validates PWYC amount is within tier bounds. Requires
        event.can_attend_without_login=True. Returns 400 if event doesn't allow guest access, if a
        non-guest account exists, or if PWYC amount is invalid.

        **Request Body:**
        - `email`: Guest user's email address
        - `first_name`: Guest user's first name
        - `last_name`: Guest user's last name
        - `tickets`: List of tickets to purchase, each with:
          - `guest_name`: Name of the ticket holder (required)
          - `seat_id`: Seat UUID for USER_CHOICE seat assignment mode (optional)
        - `price_per_ticket`: PWYC amount per ticket (same for all tickets in batch)

        **Seat Assignment Modes:**
        - `NONE`: No seat assigned (general admission)
        - `BEST_AVAILABLE`: System auto-assigns the best adjacent block of seats
        - `USER_CHOICE`: User must provide seat_id for each ticket

        **Online tiers:** returns `requires_payment=true` and a `reservation_id`. Call
        `POST /events/reservations/{reservation_id}/checkout-session/public` next to
        obtain the Stripe `checkout_url`. Free / offline / at-the-door tiers complete
        here (`requires_payment=false`, email confirmation sent).
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.maybe_user()),
            pk=tier_id,
            event=event,
        )
        if tier.price_type != models.TicketTier.PriceType.PWYC:
            raise HttpError(400, str(_("This endpoint is only for pay-what-you-can tickets")))
        return guest_service.handle_guest_ticket_checkout(
            event,
            tier,
            payload.email,
            payload.first_name,
            payload.last_name,
            payload.tickets,
            pwyc_amount=payload.price_per_ticket,
            billing_info=payload.billing_info,
            guest_session=self._resolve_guest_session(),
        )

    @route.post(
        "/reservations/{uuid:reservation_id}/checkout-session/public",
        url_name="guest_checkout_session",
        response={200: schema.CheckoutSessionResponse, 404: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_checkout_session(self, reservation_id: UUID) -> schema.CheckoutSessionResponse:
        """Create the Stripe checkout session for a guest reservation (#632).

        Second step of guest online checkout. The `reservation_id` is an
        unguessable bearer handle returned by the guest checkout endpoints.
        Idempotent; returns 404 if unknown/expired.
        """
        from events.service import stripe_service

        # The bearer handle only unlocks guest-originated reservations: an
        # authenticated user's reservation must not be redeemable on this
        # unauthenticated route (its own endpoint enforces ownership).
        if not stripe_service.reservation_owned_by(reservation_id, None):
            raise HttpError(404, str(_("No pending reservation found.")))
        return schema.CheckoutSessionResponse(
            checkout_url=stripe_service.create_batch_session(reservation_id=reservation_id)
        )
