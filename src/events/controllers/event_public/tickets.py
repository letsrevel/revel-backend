import typing as t
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
from events.service import discount_code_service, ticket_service
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
    def list_tiers(self, event_id: UUID) -> list[models.TicketTier]:
        """Get all visible ticket tiers for this event.

        Returns ticket types with pricing, availability, and sales windows. Each tier includes
        a `can_purchase` boolean indicating whether the current user is eligible to buy from it.
        Tiers the user cannot see at all (e.g. STAFF_ONLY) are excluded entirely.
        """
        event = self.get_one(event_id)
        user = self.maybe_user()
        visible_tiers = list(
            models.TicketTier.objects.for_visible_event(event, user)
            .select_related("event__organization")
            .with_venue_and_sector()
            .distinct()
            .order_by("display_order", "name")
        )
        if user and not user.is_anonymous:
            eligible_ids = {tier.id for tier in ticket_service.get_eligible_tiers(event, user)}
        else:
            # Anonymous users can only purchase PUBLIC tiers
            eligible_ids = {
                tier.id for tier in visible_tiers if tier.purchasable_by == models.TicketTier.PurchasableBy.PUBLIC
            }
        for tier in visible_tiers:
            tier._can_purchase = tier.id in eligible_ids  # type: ignore[attr-defined]
        return visible_tiers

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

        # Validate discount code if provided
        dc = None
        price_override = None
        if payload.discount_code:
            dc = discount_code_service.validate_discount_code(
                payload.discount_code, event.organization, tier, user, len(payload.tickets)
            )
            price_override = discount_code_service.calculate_discounted_price(tier, dc)

        # Create batch of tickets
        service = BatchTicketService(event, tier, user, discount_code=dc)
        result = service.create_batch(payload.tickets, price_override=price_override, billing_info=payload.billing_info)

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
        result = service.create_batch(
            payload.tickets, price_override=payload.price_per_ticket, billing_info=payload.billing_info
        )

        if isinstance(result, str):
            return schema.BatchCheckoutResponse(checkout_url=result, tickets=[])
        return schema.BatchCheckoutResponse(
            checkout_url=None,
            tickets=[schema.UserTicketSchema.from_orm(t) for t in result],
        )

    @route.post(
        "/{uuid:event_id}/tickets/vat-preview",
        url_name="vat_preview",
        response={200: schema.VATPreviewResponseSchema},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def vat_preview(
        self,
        event_id: UUID,
        payload: schema.VATPreviewRequestSchema,
    ) -> schema.VATPreviewResponseSchema:
        """Preview VAT breakdown based on buyer billing info.

        Validates the buyer's VAT ID (via VIES with caching) and calculates
        per-line-item and total VAT breakdown. Used by the frontend to display
        adjusted prices before Stripe checkout.
        """
        from events.service.attendee_vat_service import calculate_vat_preview

        event = self.get_one(event_id)
        result = calculate_vat_preview(
            event,
            payload.billing_info,
            payload.items,
            discount_code=payload.discount_code,
            price_per_ticket=payload.price_per_ticket,
        )

        return schema.VATPreviewResponseSchema(
            vat_id_valid=result.vat_id_valid,
            vat_id_validation_error=result.vat_id_validation_error,
            reverse_charge=result.reverse_charge,
            line_items=[
                schema.VATPreviewLineItemSchema(
                    tier_name=li.tier_name,
                    ticket_count=li.ticket_count,
                    unit_price_gross=li.unit_price_gross,
                    unit_price_net=li.unit_price_net,
                    unit_vat=li.unit_vat,
                    vat_rate=li.vat_rate,
                    line_net=li.line_net,
                    line_vat=li.line_vat,
                    line_gross=li.line_gross,
                )
                for li in result.line_items
            ],
            total_net=result.total_net,
            total_vat=result.total_vat,
            total_gross=result.total_gross,
            currency=result.currency,
        )

    @route.post(
        "/{uuid:event_id}/tickets/{tier_id}/validate-discount",
        url_name="validate_discount_code",
        response={200: schema.DiscountCodeValidationResponse},
        throttle=WriteThrottle(),
    )
    def validate_discount(
        self,
        event_id: UUID,
        tier_id: UUID,
        payload: schema.DiscountCodeValidationSchema,
    ) -> schema.DiscountCodeValidationResponse:
        """Validate a discount code and preview the discounted price.

        Works for both authenticated and guest users. Does not decrement usage - preview only.
        Returns whether the code is valid and what the discounted price would be.
        """
        event = self.get_one(event_id)
        user = self.maybe_user()
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(user),
            pk=tier_id,
            event=event,
        )

        try:
            return discount_code_service.preview_discount_code(payload.code, event.organization, tier, user)
        except HttpError as e:
            return schema.DiscountCodeValidationResponse(
                valid=False,
                message=str(e.message),
            )

    @route.get(
        "/tickets/{ticket_id}/cancellation-preview",
        url_name="ticket_cancellation_preview",
        response={200: schema.CancellationPreviewSchema, 403: ResponseMessage},
        auth=I18nJWTAuth(),
    )
    def cancellation_preview(self, ticket_id: UUID) -> schema.CancellationPreviewSchema:
        """Preview the refund the ticket holder would get if they cancelled now.

        Returns 403 if the caller is not the ticket owner.
        When ``can_cancel`` is False, ``reason`` is populated with a stable error code
        that the frontend can use for i18n.
        """
        from django.utils import timezone

        from events.service.cancellation_service import build_cancellation_preview

        ticket = get_object_or_404(models.Ticket.objects.full(), pk=ticket_id)
        user = self.user()
        if ticket.user_id != user.id:
            raise HttpError(403, str(_("Only the ticket holder can view this preview.")))

        preview = build_cancellation_preview(ticket, timezone.now())
        return schema.CancellationPreviewSchema(
            can_cancel=preview.can_cancel,
            reason=preview.reason,
            refund_amount=preview.refund_amount,
            currency=preview.currency,
            deadline=preview.deadline,
            flat_fee=preview.flat_fee,
            payment_method=models.TicketTier.PaymentMethod(preview.payment_method),
            windows=[
                schema.RefundWindowSchema(
                    refund_percentage=w.refund_percentage,
                    refund_amount=w.refund_amount,
                    effective_until=w.effective_until,
                )
                for w in preview.windows
            ],
            policy_snapshot=(
                schema.RefundPolicySchema.model_validate(preview.policy_snapshot.model_dump())
                if preview.policy_snapshot is not None
                else None
            ),
        )

    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_my_ticket",
        response={
            200: schema.TicketCancellationResponseSchema,
            403: ResponseMessage,
            409: schema.CancellationBlockedErrorSchema,
            502: ResponseMessage,
        },
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def cancel_my_ticket(
        self,
        ticket_id: UUID,
        payload: schema.TicketCancellationRequestSchema,
    ) -> t.Any:
        """Ticket-holder-initiated cancellation with automatic Stripe refund where applicable.

        Returns 200 with ``TicketCancellationResponseSchema`` on success, 403 if caller
        is not the ticket owner, 409 with a stable error code if the ticket cannot be
        cancelled (already cancelled, event started, cancellation disabled on tier, etc.),
        or 502 if a Stripe refund request fails.
        """
        from django.utils import timezone

        from events.service.cancellation_service import (
            CancellationBlocked,
            CancellationNotOwner,
            StripeRefundFailed,
            cancel_ticket_by_user,
        )

        ticket = get_object_or_404(models.Ticket.objects.full(), pk=ticket_id)
        user = self.user()

        try:
            result = cancel_ticket_by_user(
                ticket=ticket,
                user=user,
                reason=payload.reason or "",
                now=timezone.now(),
            )
        except CancellationNotOwner as exc:
            raise HttpError(403, str(_("Only the ticket holder can cancel this ticket."))) from exc
        except CancellationBlocked as exc:
            return 409, schema.CancellationBlockedErrorSchema(
                code=exc.reason,
                detail=str(exc.reason.label),
            )
        except StripeRefundFailed as exc:
            raise HttpError(502, str(_("Refund failed. Please try again later."))) from exc

        fresh = models.Ticket.objects.full().get(pk=result.ticket.pk)
        return schema.TicketCancellationResponseSchema(
            ticket=schema.UserTicketSchema.from_orm(fresh),
            refund_amount=result.refund_amount,
            currency=result.currency,
            refund_status=(models.Payment.RefundStatus(result.refund_status) if result.refund_status else None),
        )
