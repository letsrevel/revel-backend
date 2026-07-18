from uuid import UUID

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.throttling import WriteThrottle
from events import schema
from events.controllers.permissions import EventPermission
from events.service.seating import overrides as overrides_service

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("edit_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminSeatingController(EventAdminBaseController):
    """Box-office seat overrides: bulk hold/kill/release with reasons."""

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
