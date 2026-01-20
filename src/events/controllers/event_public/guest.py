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

from .base import EventPublicBaseController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventPublicGuestController(EventPublicBaseController):
    """Handles guest user (unauthenticated) checkout and RSVP operations."""

    @route.post(
        "/{uuid:event_id}/rsvp/{answer}/public",
        url_name="guest_rsvp",
        response={200: schema.GuestActionResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_rsvp(
        self, event_id: UUID, answer: models.EventRSVP.RsvpStatus, payload: schema.GuestUserDataSchema
    ) -> schema.GuestActionResponseSchema:
        """RSVP to an event without authentication (guest user).

        Creates or updates a guest user and sends a confirmation email. The RSVP is created only
        after the user confirms via the email link. Requires event.can_attend_without_login=True.
        Returns 400 if event doesn't allow guest access or if a non-guest account exists with
        the provided email.
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        return guest_service.handle_guest_rsvp(event, answer, payload.email, payload.first_name, payload.last_name)

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
        - `RANDOM`: System auto-assigns available seats
        - `USER_CHOICE`: User must provide seat_id for each ticket
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
            event, tier, payload.email, payload.first_name, payload.last_name, payload.tickets
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
        - `RANDOM`: System auto-assigns available seats
        - `USER_CHOICE`: User must provide seat_id for each ticket
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
        )
