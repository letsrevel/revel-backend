from uuid import UUID

from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import (
    api_controller,
    route,
)

from common.authentication import I18nJWTAuth, OptionalAuth
from common.schema import ResponseMessage
from common.throttling import WriteThrottle
from events import models, schema
from events.controllers.permissions import CanPurchaseTicket
from events.service.batch_ticket_service import BatchTicketService
from events.service.event_manager import EventManager, EventUserEligibility

from .base import EventPublicBaseController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventPublicTicketsController(EventPublicBaseController):
    """Handles ticket tiers and authenticated checkout operations."""

    @route.get(
        "/{uuid:event_id}/tickets/tiers",
        url_name="tier_list",
        response={200: list[schema.TicketTierSchema]},
    )
    def list_tiers(self, event_id: UUID) -> models.ticket.TicketTierQuerySet:
        """Get all ticket tiers available for purchase at this event.

        Returns ticket types with pricing, availability, and sales windows. Filters tiers based
        on user eligibility - you'll only see tiers you're allowed to purchase. Check visibility
        settings and sales_start_at/sales_end_at to determine which are currently on sale.
        """
        event = self.get_one(event_id)
        return (
            models.TicketTier.objects.for_user(self.maybe_user()).filter(event=event).with_venue_and_sector().distinct()
        )

    @route.get(
        "/{uuid:event_id}/tickets/{tier_id}/seats",
        url_name="tier_seat_availability",
        response={200: schema.SectorAvailabilitySchema, 404: ResponseMessage},
    )
    def get_tier_seat_availability(self, event_id: UUID, tier_id: UUID) -> schema.SectorAvailabilitySchema:
        """Get available seats for a ticket tier with seat assignment.

        Returns seat availability for tiers that have seat assignment (RANDOM or USER_CHOICE mode).
        Useful for displaying a seat map where users can select seats.

        **Returns:**
        - Sector info with shape coordinates and metadata for rendering
        - List of all seats with their availability status (available=True/False)
        - Available/total seat counts

        **Seat Status:**
        - `available=True`: Seat can be selected
        - `available=False`: Already taken by PENDING or ACTIVE ticket

        Returns 404 if the tier doesn't have seat assignment (NONE mode) or no sector is assigned.
        """
        from events.service import venue_service

        event = self.get_one(event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.maybe_user()),
            pk=tier_id,
            event=event,
        )

        return venue_service.get_tier_seat_availability(event, tier)

    @route.post(
        "/{uuid:event_id}/tickets/{tier_id}/checkout",
        url_name="ticket_checkout",
        response={200: schema.BatchCheckoutResponse, 400: EventUserEligibility},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
        permissions=[CanPurchaseTicket()],
    )
    def ticket_checkout(
        self,
        event_id: UUID,
        tier_id: UUID,
        payload: schema.BatchCheckoutPayload,
    ) -> schema.BatchCheckoutResponse:
        """Purchase one or more fixed-price event tickets.

        Supports batch purchases with individual guest names per ticket. Runs eligibility checks
        before allowing purchase. For online payment: returns Stripe checkout URL to redirect
        user for payment. For free/offline/at-the-door tickets: creates tickets immediately.

        Cannot be used for pay-what-you-can (PWYC) tiers - use the /checkout/pwyc endpoint instead.

        **Request Body:**
        - `tickets`: List of tickets to purchase, each with:
          - `guest_name`: Name of the ticket holder (required)
          - `seat_id`: Seat UUID for USER_CHOICE seat assignment mode (optional)

        **Seat Assignment Modes:**
        - `NONE`: No seat assigned (general admission)
        - `RANDOM`: System auto-assigns available seats
        - `USER_CHOICE`: User must provide seat_id for each ticket

        On eligibility failure, returns 400 with eligibility details explaining what's blocking
        you and what next_step to take.
        """
        event = get_object_or_404(self.get_queryset(include_past=True), pk=event_id)
        user = self.user()
        # Use for_visible_event() to avoid redundant Event.for_user() call
        # since we already have a visibility-checked event
        tier = get_object_or_404(
            models.TicketTier.objects.for_visible_event(event, user),
            pk=tier_id,
        )

        if tier.price_type == models.TicketTier.PriceType.PWYC:
            raise HttpError(400, str(_("Use /checkout/pwyc endpoint for pay-what-you-can tickets")))

        # Run eligibility check
        manager = EventManager(user, event)
        manager.check_eligibility(raise_on_false=True)

        # Create batch of tickets
        service = BatchTicketService(event, tier, user)
        result = service.create_batch(payload.tickets)

        if isinstance(result, str):
            return schema.BatchCheckoutResponse(checkout_url=result, tickets=[])
        return schema.BatchCheckoutResponse(
            checkout_url=None,
            tickets=[schema.UserTicketSchema.from_orm(t) for t in result],
        )

    @route.post(
        "/{uuid:event_id}/tickets/{tier_id}/checkout/pwyc",
        url_name="ticket_pwyc_checkout",
        response={200: schema.BatchCheckoutResponse, 400: EventUserEligibility},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
        permissions=[CanPurchaseTicket()],
    )
    def ticket_pwyc_checkout(
        self,
        event_id: UUID,
        tier_id: UUID,
        payload: schema.BatchCheckoutPWYCPayload,
    ) -> schema.BatchCheckoutResponse:
        """Purchase one or more pay-what-you-can (PWYC) tickets.

        Only works for ticket tiers with price_type=PWYC. All tickets in the batch are purchased
        at the same price_per_ticket amount. Validates the amount is within the tier's min/max
        bounds.

        **Request Body:**
        - `tickets`: List of tickets to purchase, each with:
          - `guest_name`: Name of the ticket holder (required)
          - `seat_id`: Seat UUID for USER_CHOICE seat assignment mode (optional)
        - `price_per_ticket`: PWYC amount per ticket (same for all tickets in batch)

        Returns Stripe checkout URL for online payment, or creates tickets immediately for
        free/offline payment methods. Returns 400 for non-PWYC tiers, if amount is out of
        bounds, or on eligibility failure.
        """
        event = get_object_or_404(self.get_queryset(include_past=True), pk=event_id)
        user = self.user()
        # Use for_visible_event() to avoid redundant Event.for_user() call
        tier = get_object_or_404(
            models.TicketTier.objects.for_visible_event(event, user),
            pk=tier_id,
        )

        # Validate that this tier is actually PWYC
        if tier.price_type != models.TicketTier.PriceType.PWYC:
            raise HttpError(400, str(_("This endpoint is only for pay-what-you-can tickets")))

        # Validate PWYC amount is within bounds
        if payload.price_per_ticket < tier.pwyc_min:
            raise HttpError(
                400,
                str(_("PWYC amount must be at least {min_amount}")).format(min_amount=tier.pwyc_min),
            )

        if tier.pwyc_max and payload.price_per_ticket > tier.pwyc_max:
            raise HttpError(
                400,
                str(_("PWYC amount must be at most {max_amount}")).format(max_amount=tier.pwyc_max),
            )

        # Run eligibility check
        manager = EventManager(user, event)
        manager.check_eligibility(raise_on_false=True)

        # Create batch of tickets
        service = BatchTicketService(event, tier, user)
        result = service.create_batch(payload.tickets, price_override=payload.price_per_ticket)

        if isinstance(result, str):
            return schema.BatchCheckoutResponse(checkout_url=result, tickets=[])
        return schema.BatchCheckoutResponse(
            checkout_url=None,
            tickets=[schema.UserTicketSchema.from_orm(t) for t in result],
        )
