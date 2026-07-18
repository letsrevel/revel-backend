from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.throttling import WriteThrottle
from events import models, schema
from events.controllers.permissions import EventPermission
from events.service.seating import box_office
from events.service.seating import overrides as overrides_service

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("manage_tickets")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminSeatingController(EventAdminBaseController):
    """Box-office seat overrides, door sales/comps, and reseat."""

    @route.put(
        "/seating/overrides",
        url_name="event_seating_overrides",
        response=schema.SeatOverridesResponse,
    )
    def apply_overrides(self, event_id: UUID, payload: schema.SeatOverridesRequest) -> schema.SeatOverridesResponse:
        """Bulk-apply seat overrides for this event.

        Seats in ``set`` are held/killed (upsert); ``release_seat_ids`` clear their
        override. Seats holding a live ticket on this event are rejected per-seat
        (into ``rejected``), never the whole batch.
        """
        event = self.get_one(event_id)
        return overrides_service.apply_overrides(
            event,
            set_items=[(i.seat_id, i.status.value, i.reason) for i in payload.set],
            release_seat_ids=payload.release_seat_ids,
        )

    @route.post(
        "/seating/sell",
        url_name="event_seating_sell",
        response=schema.AdminTicketSchema,
    )
    def box_office_sell(self, event_id: UUID, payload: schema.BoxOfficeSellRequest) -> models.Ticket:
        """Door sale / comp: issue an ACTIVE ticket directly on a seat.

        The recipient is an email (guest user get-or-create) or an existing
        ``user_id``. A box-office HELD override on the seat is released as part
        of the sale; a KILLED seat is rejected.
        """
        event = self.get_one(event_id)
        tier = get_object_or_404(models.TicketTier, pk=payload.tier_id, event=event)
        recipient = box_office.resolve_recipient(payload.email, payload.user_id, payload.first_name, payload.last_name)
        ticket = box_office.sell(
            event,
            tier,
            seat_id=payload.seat_id,
            payment_method=payload.payment_method,
            recipient=recipient,
            guest_name=payload.guest_name,
        )
        return models.Ticket.objects.full().get(pk=ticket.pk)

    @route.post(
        "/seating/reseat",
        url_name="event_seating_reseat",
        response=schema.AdminTicketSchema,
    )
    def box_office_reseat(self, event_id: UUID, payload: schema.BoxOfficeReseatRequest) -> models.Ticket:
        """Move a PENDING/ACTIVE ticket to another free seat in the same price category."""
        event = self.get_one(event_id)
        ticket = box_office.reseat(event, ticket_id=payload.ticket_id, target_seat_id=payload.target_seat_id)
        return models.Ticket.objects.full().get(pk=ticket.pk)
