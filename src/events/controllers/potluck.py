from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_jwt.authentication import JWTAuth

from common.throttling import UserDefaultThrottle, WriteThrottle
from events import schema
from events.models import Event, PotluckItem
from events.service import potluck_service, update_db_instance

from .permissions import ManagePotluckPermission, PotluckItemPermission
from .user_aware_controller import UserAwareController


@api_controller("/events/{event_id}/potluck", auth=JWTAuth(), tags=["Potluck"], throttle=WriteThrottle())
class PotluckController(UserAwareController):
    @route.get(
        "/",
        url_name="list_potluck_items",
        response=list[schema.PotluckItemRetrieveSchema],
        throttle=UserDefaultThrottle(),
    )
    def list_potluck_items(self, event_id: UUID) -> QuerySet[PotluckItem]:
        """List potluck items for an event."""
        event = self.get_object_or_exception(self.get_event_queryset(), pk=event_id)
        return PotluckItem.objects.filter(event=event).select_related("created_by", "assignee")

    @route.post(
        "/",
        url_name="create_potluck_item",
        response=schema.PotluckItemRetrieveSchema,
        permissions=[PotluckItemPermission("create_potluck_item")],
    )
    def create_potluck_item(self, event_id: UUID, payload: schema.PotluckItemCreateSchema) -> PotluckItem:
        """Create a potluck item."""
        event = self.get_object_or_exception(self.get_event_queryset(), pk=event_id)
        return potluck_service.create_potluck_item(event, **payload.model_dump(), created_by=self.user())

    @route.put(
        "/{item_id}",
        url_name="update_potluck_item",
        response=schema.PotluckItemRetrieveSchema,
        permissions=[ManagePotluckPermission()],
    )
    def update_potluck_item(
        self, event_id: UUID, item_id: UUID, payload: schema.PotluckItemCreateSchema
    ) -> PotluckItem:
        """Update a potluck item."""
        event = self.get_event(event_id)
        potluck_item = self.get_object_or_exception(PotluckItem, id=item_id, event=event)
        return update_db_instance(potluck_item, payload)  # type: ignore[no-any-return]

    @route.delete(
        "/{item_id}", url_name="delete_potluck_item", permissions=[ManagePotluckPermission()], response={204: None}
    )
    def delete_potluck_item(self, event_id: UUID, item_id: UUID) -> None:
        """Delete a potluck item."""
        event = self.get_event(event_id)
        potluck_item = self.get_object_or_exception(PotluckItem, id=item_id, event=event)
        potluck_item.delete()

    @route.post(
        "/{item_id}/claim",
        url_name="claim_potluck_item",
        response=schema.PotluckItemRetrieveSchema,
        permissions=[PotluckItemPermission("claim_potluck_item")],
    )
    def claim_potluck_item(self, event_id: UUID, item_id: UUID) -> PotluckItem:
        """Claim a potluck item."""
        event = self.get_object_or_exception(self.get_event_queryset(), pk=event_id)
        potluck_item = get_object_or_404(PotluckItem, id=item_id, event=event)
        return potluck_service.claim_potluck_item(potluck_item, self.user())

    def get_event_queryset(self, include_past: bool = False) -> QuerySet[Event]:
        """Get the event queryset."""
        return Event.objects.for_user(self.user(), include_past=include_past)

    def get_event(self, event_id: UUID) -> Event:
        """Get event by slug."""
        return get_object_or_404(self.get_event_queryset(), pk=event_id)
