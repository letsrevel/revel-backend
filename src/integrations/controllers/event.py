"""Event-level listing management: push, publish, auto-sync override (spec §11)."""

from uuid import UUID

from django.http import HttpResponse
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events.controllers.event_admin.base import EventAdminBaseController
from events.controllers.permissions import EventPermission
from integrations import schema
from integrations.service import sync_service


@api_controller(
    "/event-admin/{event_id}/integrations",
    auth=I18nJWTAuth(),
    tags=["Event Admin"],
    throttle=UserDefaultThrottle(),
    permissions=[EventPermission("manage_event")],
)
class EventIntegrationsController(EventAdminBaseController):
    """Mirror an event onto connected platforms."""

    @route.get("", url_name="list_event_integrations", response=list[schema.EventLinkSchema])
    def list_links(self, event_id: UUID) -> list[schema.EventLinkSchema]:
        """Every platform link this event has, with sync state and report."""
        return sync_service.list_links(self.get_one(event_id))

    @route.post(
        "/{provider}/push",
        url_name="event_integration_push",
        response={202: schema.EventLinkSchema},
        throttle=WriteThrottle(),
    )
    def push(self, event_id: UUID, provider: str) -> HttpResponse:
        """Queue a full-state push. Returns the link in ``pending`` state; poll the list endpoint."""
        link = sync_service.request_push(self.get_one(event_id), provider)
        return self.create_response(sync_service.to_link_schema(link), status_code=202)

    @route.post(
        "/{provider}/publish",
        url_name="event_integration_publish",
        response=schema.EventLinkSchema,
        throttle=WriteThrottle(),
    )
    def publish(self, event_id: UUID, provider: str) -> schema.EventLinkSchema:
        """Make the remote draft live. Synchronous; the platform's refusal comes back as 502 with its message."""
        return sync_service.to_link_schema(sync_service.publish_link(self.get_one(event_id), provider))

    @route.patch(
        "/{provider}", url_name="event_integration_update", response=schema.EventLinkSchema, throttle=WriteThrottle()
    )
    def update(self, event_id: UUID, provider: str, payload: schema.EventLinkUpdateSchema) -> schema.EventLinkSchema:
        """Per-event auto-sync override (null inherits the connection default)."""
        return sync_service.to_link_schema(
            sync_service.set_link_auto_sync(self.get_one(event_id), provider, payload.auto_sync)
        )
