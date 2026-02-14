from uuid import UUID

from django.db import transaction
from django.db.models import F, QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Body, Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.schema import ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.service import ticket_service

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminTicketsController(EventAdminBaseController):
    """Event ticket tier and ticket management endpoints."""

    # ---- Ticket Tiers ----

    @route.get(
        "/ticket-tiers",
        url_name="list_ticket_tiers",
        response=PaginatedResponseSchema[schema.TicketTierDetailSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_ticket_tiers(self, event_id: UUID) -> QuerySet[models.TicketTier]:
        """List all ticket tiers for an event."""
        self.get_one(event_id)
        return models.TicketTier.objects.with_venue_and_sector().filter(event_id=event_id).distinct()

    @route.post(
        "/ticket-tier",
        url_name="create_ticket_tier",
        response=schema.TicketTierDetailSchema,
        permissions=[EventPermission("manage_tickets")],
    )
    def create_ticket_tier(self, event_id: UUID, payload: schema.TicketTierCreateSchema) -> models.TicketTier:
        """Create a new ticket tier for an event."""
        event = self.get_one(event_id)
        if (
            payload.payment_method == models.TicketTier.PaymentMethod.ONLINE
            and not event.organization.is_stripe_connected
        ):
            raise HttpError(400, str(_("You must connect to Stripe first.")))

        # Extract restricted_to_membership_tiers_ids from payload
        payload_dict = payload.model_dump(exclude_unset=True)
        restricted_to_membership_tiers_ids = payload_dict.pop("restricted_to_membership_tiers_ids", None)

        # Create ticket tier with M2M handling in service layer
        tier = ticket_service.create_ticket_tier(
            event=event, tier_data=payload_dict, restricted_to_membership_tiers_ids=restricted_to_membership_tiers_ids
        )
        # Refetch with venue/sector for response serialization
        return models.TicketTier.objects.with_venue_and_sector().get(pk=tier.pk)

    @route.put(
        "/ticket-tier/{tier_id}",
        url_name="update_ticket_tier",
        response=schema.TicketTierDetailSchema,
        permissions=[EventPermission("manage_tickets")],
    )
    def update_ticket_tier(
        self, event_id: UUID, tier_id: UUID, payload: schema.TicketTierUpdateSchema
    ) -> models.TicketTier:
        """Update a ticket tier."""
        event = self.get_one(event_id)
        if (
            payload.payment_method == models.TicketTier.PaymentMethod.ONLINE
            and not event.organization.is_stripe_connected
        ):
            raise HttpError(400, str(_("You must connect to Stripe first.")))

        tier = get_object_or_404(models.TicketTier, pk=tier_id, event=event)

        # Extract restricted_to_membership_tiers_ids from payload
        payload_dict = payload.model_dump(exclude_unset=True)
        restricted_to_membership_tiers_ids = payload_dict.pop("restricted_to_membership_tiers_ids", None)

        # Update ticket tier with M2M handling in service layer
        updated_tier = ticket_service.update_ticket_tier(
            tier=tier, tier_data=payload_dict, restricted_to_membership_tiers_ids=restricted_to_membership_tiers_ids
        )
        # Refetch with venue/sector for response serialization
        return models.TicketTier.objects.with_venue_and_sector().get(pk=updated_tier.pk)

    @route.delete(
        "/ticket-tier/{tier_id}",
        url_name="delete_ticket_tier",
        response={204: None},
        permissions=[EventPermission("manage_tickets")],
    )
    def delete_ticket_tier(self, event_id: UUID, tier_id: UUID) -> tuple[int, None]:
        """Delete a ticket tier.

        Note this might raise a 400 if ticket with this tier where already bought.
        """
        event = self.get_one(event_id)
        tier = get_object_or_404(models.TicketTier, pk=tier_id, event=event)
        tier.delete()
        return 204, None

    @route.patch(
        "/ticket-tiers/reorder",
        url_name="reorder_ticket_tiers",
        response={204: None},
        permissions=[EventPermission("manage_tickets")],
    )
    def reorder_ticket_tiers(self, event_id: UUID, payload: schema.ReorderSchema) -> tuple[int, None]:
        """Reorder ticket tiers for an event."""
        event = self.get_one(event_id)
        ticket_service.reorder_ticket_tiers(event, payload.tier_ids)
        return 204, None

    # ---- Tickets ----

    @route.get(
        "/tickets",
        url_name="list_tickets",
        response=PaginatedResponseSchema[schema.AdminTicketSchema],
        permissions=[EventPermission("manage_tickets")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=["user__email", "user__first_name", "user__last_name", "tier__name", "user__preferred_name"],
    )
    def list_tickets(
        self,
        event_id: UUID,
        params: filters.TicketFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Ticket]:
        """List tickets for an event with optional filters.

        Supports filtering by:
        - status: Filter by ticket status (PENDING, ACTIVE, CANCELLED, CHECKED_IN)
        - tier__payment_method: Filter by payment method (ONLINE, OFFLINE, AT_THE_DOOR, FREE)
        """
        event = self.get_one(event_id)
        # Use full() for AdminTicketSchema (includes user, tier, venue, sector, seat, payment)
        # with_org_membership() prefetches user's membership for "Make Member" feature
        qs = models.Ticket.objects.full().with_org_membership(event.organization_id).filter(event=event)
        return params.filter(qs).distinct()

    @route.get(
        "/tickets/{ticket_id}",
        url_name="get_ticket",
        response={200: schema.AdminTicketSchema},
        permissions=[EventPermission("manage_tickets")],
        throttle=UserDefaultThrottle(),
    )
    def get_ticket(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Get a ticket by its ID."""
        event = self.get_one(event_id)
        return get_object_or_404(models.Ticket.objects.full(), pk=ticket_id, event=event)

    @route.post(
        "/tickets/{ticket_id}/confirm-payment",
        url_name="confirm_ticket_payment",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def confirm_ticket_payment(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: schema.ConfirmPaymentSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.Ticket:
        """Confirm payment for a pending offline ticket and activate it."""
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier"),
            pk=ticket_id,
            event=event,
            status=models.Ticket.TicketStatus.PENDING,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )
        return ticket_service.confirm_ticket_payment(ticket, price_paid=payload.price_paid if payload else None)

    @route.post(
        "/tickets/{ticket_id}/unconfirm-payment",
        url_name="unconfirm_ticket_payment",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def unconfirm_ticket_payment(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Revert a confirmed ticket back to pending status.

        Only applies to OFFLINE payment method. AT_THE_DOOR tickets are always
        ACTIVE (commitment to attend) and should not be reverted to PENDING.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket,
            pk=ticket_id,
            event=event,
            status=models.Ticket.TicketStatus.ACTIVE,
            tier__payment_method=models.TicketTier.PaymentMethod.OFFLINE,
        )
        return ticket_service.unconfirm_ticket_payment(ticket)

    @route.post(
        "/tickets/{ticket_id}/mark-refunded",
        url_name="mark_ticket_refunded",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def mark_ticket_refunded(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Mark a manual payment ticket as refunded and cancel it.

        This endpoint is for offline/at-the-door tickets only.
        Online tickets (Stripe) are automatically managed via webhooks.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier", "payment"),
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )

        # Restore ticket quantity and cancel the ticket
        with transaction.atomic():
            models.TicketTier.objects.select_for_update().filter(pk=ticket.tier.pk, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            ticket.status = models.Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

            # Mark the associated payment as refunded if it exists
            if hasattr(ticket, "payment"):
                ticket.payment.status = models.Payment.PaymentStatus.REFUNDED
                ticket.payment.save(update_fields=["status"])

        # Refund notification sent automatically by stripe webhook handler
        # Re-fetch with full() to include all related objects for UserTicketSchema
        return models.Ticket.objects.full().get(pk=ticket.pk)

    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_ticket",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def cancel_ticket(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Cancel a manual payment ticket.

        This endpoint is for offline/at-the-door tickets only.
        Online tickets (Stripe) should be refunded via Stripe Dashboard.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier"),
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )

        if ticket.status == models.Ticket.TicketStatus.CANCELLED:
            raise HttpError(400, str(_("Ticket already cancelled")))

        # Restore ticket quantity and cancel the ticket
        with transaction.atomic():
            models.TicketTier.objects.select_for_update().filter(pk=ticket.tier.pk, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            # Store old status before updating (signal handler needs this)
            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
            ticket.status = models.Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

        # Notification sent automatically via signal handler
        # Re-fetch with full() to include all related objects for UserTicketSchema
        return models.Ticket.objects.full().get(pk=ticket.pk)

    @route.post(
        "/tickets/{ticket_id}/check-in",
        url_name="check_in_ticket",
        response={200: schema.CheckInResponseSchema, 400: ValidationErrorResponse},
        permissions=[EventPermission("check_in_attendees")],
    )
    def check_in_ticket(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: schema.ConfirmPaymentSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.Ticket:
        """Check in an attendee by scanning their ticket."""
        event = self.get_one(event_id)
        return ticket_service.check_in_ticket(
            event, ticket_id, self.user(), price_paid=payload.price_paid if payload else None
        )
