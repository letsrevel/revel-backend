import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from ninja import Body, Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.schema import ValidationErrorResponse
from common.throttling import ExportThrottle, UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.service import ticket_service

from .base import EventAdminBaseController

if t.TYPE_CHECKING:
    from common.models import FileExport

TicketOrdering = t.Literal[
    "created_at",
    "-created_at",
    "tier__name",
    "-tier__name",
    "status",
    "-status",
    "tier__payment_method",
    "-tier__payment_method",
    "price",
    "-price",
    "price_paid",
    "-price_paid",
]

# Effective amount actually taken per ticket: the Stripe payment amount (online),
# else the recorded PWYC amount (offline/at-the-door), else the tier list price
# (fixed-price tiers, where neither of the former is set). tier.price is non-nullable
# (defaults to 0), so the result is never NULL — no NULLS FIRST/LAST handling needed.
# All three operands are already joined via Ticket.objects.full(), but the COALESCE
# itself must be annotated so it appears in the SELECT list (required for SELECT DISTINCT).
EFFECTIVE_PRICE_PAID = Coalesce("payment__amount", "price_paid", "tier__price")

# Maps the public ``order_by`` value to the actual queryset ordering field.
TICKET_ORDER_FIELDS: dict[TicketOrdering, str] = {
    "created_at": "created_at",
    "-created_at": "-created_at",
    "tier__name": "tier__name",
    "-tier__name": "-tier__name",
    "status": "status",
    "-status": "-status",
    "tier__payment_method": "tier__payment_method",
    "-tier__payment_method": "-tier__payment_method",
    "price": "tier__price",
    "-price": "-tier__price",
    "price_paid": "effective_price_paid",
    "-price_paid": "-effective_price_paid",
}


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
        return (
            models.TicketTier.objects.with_venue_and_sector()
            .select_related("event__organization")
            .filter(event_id=event_id)
            .distinct()
        )

    @route.post(
        "/ticket-tier",
        url_name="create_ticket_tier",
        response=schema.TicketTierDetailSchema,
        permissions=[EventPermission("manage_tickets")],
    )
    def create_ticket_tier(self, event_id: UUID, payload: schema.TicketTierCreateSchema) -> models.TicketTier:
        """Create a new ticket tier for an event."""
        event = self.get_one(event_id)
        return ticket_service.create_ticket_tier(event, payload)

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
        tier = get_object_or_404(models.TicketTier, pk=tier_id, event=event)
        return ticket_service.update_ticket_tier(tier, payload)

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
        order_by: TicketOrdering = "-created_at",
    ) -> QuerySet[models.Ticket]:
        """List tickets for an event with optional filters.

        Supports filtering by:
        - status: Filter by ticket status (PENDING, ACTIVE, CANCELLED, CHECKED_IN)
        - tier__payment_method: Filter by payment method (ONLINE, OFFLINE, AT_THE_DOOR, FREE)

        Ordering (prefix with '-' for descending):
        - created_at: Purchase date (default: -created_at, newest first)
        - tier__name: Ticket tier, alphabetically
        - status: Ticket status, by stored value
        - tier__payment_method: Payment method, by stored value
        - price: Tier list price
        - price_paid: Effective amount actually paid (online payment, else PWYC amount, else tier price)
        """
        event = self.get_one(event_id)
        # Use full() for AdminTicketSchema (includes user, tier, venue, sector, seat, payment)
        # with_org_membership() prefetches user's membership for "Make Member" feature
        qs = models.Ticket.objects.full().with_org_membership(event.organization_id).filter(event=event)
        qs = params.filter(qs).annotate(effective_price_paid=EFFECTIVE_PRICE_PAID)
        # "-id" is a stable tiebreaker so pagination stays deterministic across equal sort keys.
        return qs.distinct().order_by(TICKET_ORDER_FIELDS[order_by], "-id")

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
    def mark_ticket_refunded(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: schema.AdminCancelTicketSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.Ticket:
        """Mark a manual offline/at-the-door ticket as refunded and cancel it.

        This endpoint is for manually-collected payments only. Online (Stripe) tickets
        are refunded via the Stripe Dashboard and handled automatically by webhooks.
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
        return ticket_service.mark_offline_ticket_refunded(
            ticket,
            cancelled_by=self.user(),
            reason=payload.cancellation_reason if payload else None,
        )

    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_ticket",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def cancel_ticket(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: schema.AdminCancelTicketSchema | None = Body(None),  # type: ignore[type-arg]
    ) -> models.Ticket:
        """Cancel an offline/at-the-door ticket and record organizer audit fields.

        This endpoint is for offline/at-the-door tickets only.
        Online tickets (Stripe) should be refunded via the Stripe Dashboard.
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
        return ticket_service.cancel_offline_ticket(
            ticket,
            cancelled_by=self.user(),
            reason=payload.cancellation_reason if payload else None,
        )

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

    @route.get(
        "/revenue",
        url_name="event_revenue",
        response=schema.EventRevenueSchema,
        permissions=[EventPermission("manage_tickets")],
        throttle=UserDefaultThrottle(),
    )
    def get_event_revenue(self, event_id: UUID) -> schema.EventRevenueSchema:
        """Aggregate ticket revenue for an event, grouped by currency.

        Sums online (Stripe) payments and offline/at-the-door amounts confirmed as
        paid. Online refunds are reflected in ``refunded``/``net``; offline refunds
        are not yet tracked (see #528). ``paid_ticket_count`` counts currently-held
        paid tickets.
        """
        event = self.get_one(event_id)
        revenue = ticket_service.get_event_revenue(event)
        return schema.EventRevenueSchema(
            by_currency=[schema.CurrencyRevenueSchema.model_validate(item) for item in revenue]
        )

    # ---- Export ----

    @route.post(
        "/export-attendees",
        url_name="export_attendees",
        response={202: schema.FileExportSchema},
        permissions=[EventPermission("manage_event")],
        throttle=ExportThrottle(),
    )
    def export_attendees(self, event_id: UUID) -> tuple[int, "FileExport"]:
        """Export attendee list as an Excel file (async).

        Triggers an async Celery task. Returns 202 with a FileExport resource
        that can be polled via GET /exports/{id} until the file is ready for
        download.
        Requires 'manage_event' permission.
        """
        event = self.get_one(event_id)
        return 202, ticket_service.start_attendee_export(event, requested_by=self.user())
