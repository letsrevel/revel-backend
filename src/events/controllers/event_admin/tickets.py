import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Body, Path, Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.schema import ErrorDetail, ValidationErrorResponse
from common.throttling import ExportThrottle, UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.schema.financials import EventFinancialsSchema
from events.service import refund_service, revenue_aggregation, ticket_guest_name_service, ticket_service

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
# else the recorded price (offline/at-the-door PWYC, discounted, or category-priced),
# else the tier list price. tier.price is non-nullable (defaults to 0), so the result is
# never NULL — no NULLS FIRST/LAST handling needed. All three operands are already joined
# via Ticket.objects.full(); the COALESCE is annotated so it can be used in ORDER BY.
#
# KNOWN LIMITATION — category-priced tiers (spec §5.5). This is a DB-level expression used
# to sort and display a list; it cannot walk seat → price category → the tier's
# `category_prices` JSON map the way `pricing.recorded_or_resolved_price` does per row, and
# contorting it into a JSON lookup would trade a readable ORDER BY for an unmaintainable one.
# It is therefore wrong in exactly one case: a ticket on a **category-priced tier** whose
# `price_paid` is NULL and which has no `payment` row. Every such ticket falls back to the
# flat `tier.price`, so seats in a category priced *above* the flat price are **under-reported**
# and seats priced *below* it are **over-reported** — and they sort as if they all cost the
# same. That state is only reachable for tickets sold **before** the tier opted into category
# pricing (checkout, box office and unconfirm/confirm all stamp `price_paid` now), so it is a
# bounded legacy tail, not an ongoing drift. The money-bearing paths — refund ceilings
# (`ticket_service._resolve_offline_refund_amount`) and the revenue/VAT report
# (`revenue_aggregation._process_ticket`) — resolve per row and are unaffected.
EFFECTIVE_PRICE_PAID = Coalesce("payment__amount", "price_paid", "tier__price")

# Check-in codes are a bare canonical ticket UUID (36 chars), a series pass QR payload
# ("series:" + UUID), or a membership card QR payload ("member:" + UUID) — both prefixes
# are 7 chars, so max length stays 43. See ticket_service.resolve_check_in_ticket_id()
# and ticket_service.scan_member_code(). Bounding length/shape here rejects garbage
# before it reaches the resolver (422 instead of an unbounded str hitting the ORM/service).
CHECK_IN_CODE_PATTERN = (
    r"^(series:|member:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

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
    permissions=[EventPermission("manage_tickets")],
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
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_ticket_tiers(self, event_id: UUID) -> QuerySet[models.TicketTier]:
        """List all ticket tiers for an event."""
        self.get_one(event_id)
        # No .distinct(): all joins are to-one (#880).
        return (
            models.TicketTier.objects.with_venue_and_sector()
            .select_related("event__organization")
            .filter(event_id=event_id)
        )

    @route.post(
        "/ticket-tier",
        url_name="create_ticket_tier",
        # ONLINE tiers are gated on the online-payment prerequisites: 400 when the
        # organization has no Stripe Connect, 422 when platform fees apply but its
        # billing info is incomplete.
        response={
            200: schema.TicketTierDetailSchema,
            400: ValidationErrorResponse | ErrorDetail,
            422: ValidationErrorResponse,
        },
    )
    def create_ticket_tier(self, event_id: UUID, payload: schema.TicketTierCreateSchema) -> models.TicketTier:
        """Create a new ticket tier for an event."""
        event = self.get_one(event_id)
        return ticket_service.create_ticket_tier(event, payload)

    @route.put(
        "/ticket-tier/{tier_id}",
        url_name="update_ticket_tier",
        # Same gate as create when the update switches the tier to ONLINE payment.
        response={
            200: schema.TicketTierDetailSchema,
            400: ValidationErrorResponse | ErrorDetail,
            422: ValidationErrorResponse,
        },
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
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=[
            "user__email",
            "user__first_name",
            "user__last_name",
            "tier__name",
            "user__preferred_name",
            "guest_name",
        ],
    )
    def list_tickets(
        self,
        event_id: UUID,
        params: t.Annotated[filters.TicketFilterSchema, Query(...)],
        source: t.Annotated[t.Literal["pass", "direct"] | None, Query(None)] = None,
        order_by: TicketOrdering = "-created_at",
    ) -> QuerySet[models.Ticket]:
        """List tickets for an event with optional filters.

        Supports filtering by:
        - status: Filter by ticket status (PENDING, ACTIVE, CANCELLED, CHECKED_IN)
        - tier__payment_method: Filter by payment method (ONLINE, OFFLINE, AT_THE_DOOR, FREE)
        - source: Filter by origin ("pass" for series-pass-derived tickets, "direct" for standalone purchases)

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
        if source is not None:
            qs = qs.filter(held_pass__isnull=(source == "direct"))
        qs = params.filter(qs).annotate(effective_price_paid=EFFECTIVE_PRICE_PAID)
        # "-id" is a stable tiebreaker so pagination stays deterministic across equal sort keys.
        # No .distinct(): every filter/search/order field is a to-one join, and DISTINCT over
        # the wide full() select costs ~600ms of Postgres *planning* time per request (#880).
        return qs.order_by(TICKET_ORDER_FIELDS[order_by], "-id")

    @route.get(
        "/tickets/{ticket_id}",
        url_name="get_ticket",
        response={200: schema.AdminTicketSchema},
        throttle=UserDefaultThrottle(),
    )
    def get_ticket(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Get a ticket by its ID."""
        event = self.get_one(event_id)
        return get_object_or_404(models.Ticket.objects.full(), pk=ticket_id, event=event)

    @route.patch(
        "/tickets/{ticket_id}/guest-name",
        url_name="admin_update_ticket_guest_name",
        response={200: schema.UserTicketSchema},
    )
    def update_ticket_guest_name(
        self, event_id: UUID, ticket_id: UUID, payload: schema.TicketGuestNameUpdateSchema
    ) -> models.Ticket:
        """Rename the holder on an attendee's ticket.

        Clearing the name is only allowed when the event does not require names.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("event", "tier"),
            pk=ticket_id,
            event=event,
        )
        return ticket_guest_name_service.update_guest_name(ticket, payload.guest_name)

    @route.post(
        "/tickets/{ticket_id}/confirm-payment",
        url_name="confirm_ticket_payment",
        response={200: schema.UserTicketSchema},
    )
    def confirm_ticket_payment(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: t.Annotated[schema.ConfirmPaymentSchema | None, Body(None)] = None,
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
    )
    def mark_ticket_refunded(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: t.Annotated[schema.AdminRefundTicketSchema | None, Body(None)] = None,
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
            refund_amount=payload.refund_amount if payload else None,
        )

    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_ticket",
        response={
            200: schema.UserTicketSchema,
            400: ErrorDetail,
            409: ErrorDetail,
            402: ErrorDetail,
            502: ErrorDetail,
        },
    )
    def cancel_ticket(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: t.Annotated[schema.AdminRefundTicketSchema | None, Body(None)] = None,
    ) -> models.Ticket:
        """Cancel a ticket, of any payment method, and record organizer audit fields.

        An optional ``refund_amount`` issues a Stripe refund alongside the cancellation
        for online tickets, or records a manual refund for offline/at-the-door ones.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier"),
            pk=ticket_id,
            event=event,
        )
        return refund_service.admin_cancel_ticket(
            ticket,
            cancelled_by=self.user(),
            reason=payload.cancellation_reason if payload else None,
            refund_amount=payload.refund_amount if payload else None,
        )

    @route.post(
        "/tickets/{ticket_id}/refund",
        url_name="refund_ticket_payment",
        response={
            200: schema.TicketRefundSchema,
            400: ErrorDetail,
            409: ErrorDetail,
            402: ErrorDetail,
            502: ErrorDetail,
        },
    )
    def refund_ticket_payment(
        self,
        event_id: UUID,
        ticket_id: UUID,
        payload: t.Annotated[schema.AdminIssueRefundSchema | None, Body(None)] = None,
    ) -> models.Refund:
        """Refund an online ticket payment (full or partial) without cancelling the ticket.

        Returns the ``Refund`` row in PENDING status — the ``charge.refunded`` webhook
        finalizes it to SUCCEEDED once Stripe confirms the transfer.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier", "event__organization"),
            pk=ticket_id,
            event=event,
        )
        return refund_service.issue_refund_for_ticket(
            ticket,
            amount=payload.amount if payload else None,
            initiated_by=self.user(),
            reason=(payload.reason if payload else None) or "",
            source=models.Refund.Source.ORGANIZER_API,
        )

    @route.get(
        "/tickets/{ticket_id}/refund-context",
        url_name="ticket_refund_context",
        response={200: schema.TicketRefundContextSchema},
        throttle=UserDefaultThrottle(),
    )
    def ticket_refund_context(self, event_id: UUID, ticket_id: UUID) -> schema.TicketRefundContextSchema:
        """Amounts paid/refunded/remaining plus the policy-suggested refund, for FE quick-select."""
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier", "payment", "event").prefetch_related("payment__refunds"),
            pk=ticket_id,
            event=event,
        )
        ctx = refund_service.build_refund_context(ticket, timezone.now())
        return schema.TicketRefundContextSchema(
            payment_method=ctx.payment_method,
            amount_paid=ctx.amount_paid,
            currency=ctx.currency,
            total_refunded=ctx.total_refunded,
            total_pending=ctx.total_pending,
            remaining_refundable=ctx.remaining_refundable,
            policy_suggested_amount=ctx.policy_suggested_amount,
            refunds=[schema.TicketRefundSchema.from_orm(r) for r in ctx.refunds],
        )

    @route.post(
        "/tickets/{code}/check-in",
        url_name="check_in_ticket",
        response={
            200: schema.CheckInResponseSchema | schema.MemberScanResponseSchema,
            400: ValidationErrorResponse | ErrorDetail,
        },
        permissions=[EventPermission("check_in_attendees")],
    )
    def check_in_ticket(
        self,
        event_id: UUID,
        code: t.Annotated[str, Path(..., min_length=36, max_length=43, pattern=CHECK_IN_CODE_PATTERN)],
        payload: t.Annotated[schema.ConfirmPaymentSchema | None, Body(None)] = None,
    ) -> models.Ticket | schema.MemberScanResponseSchema:
        """Check in a scanned code: ticket UUID, series pass, or membership card.

        Membership cards are report-only unless the member holds exactly one
        non-cancelled ticket for this event, in which case it is checked in.
        """
        event = self.get_one(event_id)
        price_paid = payload.price_paid if payload else None
        # String-prefix dispatch: no branch pays queries for another namespace's
        # lookup — the bare-UUID ticket path (the common case) stays query-free
        # until its own fetch inside check_in_ticket.
        if code.startswith(models.OrganizationMember.QR_PREFIX):
            result = ticket_service.scan_member_code(event, code, self.user(), price_paid=price_paid)
            if result.checked_in is not None:
                return result.checked_in
            return schema.MemberScanResponseSchema.from_result(result)
        ticket_id = ticket_service.resolve_check_in_ticket_id(event, code)
        return ticket_service.check_in_ticket(event, ticket_id, self.user(), price_paid=price_paid)

    @route.get(
        "/revenue",
        url_name="event_revenue",
        response=EventFinancialsSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_event_revenue(
        self,
        event_id: UUID,
        year: int | None = None,
        month: t.Annotated[int | None, Query(ge=1, le=12)] = None,
        quarter: t.Annotated[int | None, Query(ge=1, le=4)] = None,
    ) -> revenue_aggregation.EventFinancials:
        """Per-event financials, all-time by default; optional year/month/quarter filter."""
        event = self.get_one(event_id)
        tz = revenue_aggregation.organization_timezone(event.organization)
        date_from, date_to = revenue_aggregation.resolve_period(year, month, quarter, tz, default_all_time=True)
        scope = revenue_aggregation.ReportScope(
            org=event.organization, event_id=event.id, date_from=date_from, date_to=date_to
        )
        return revenue_aggregation.event_financials(event, scope)

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
