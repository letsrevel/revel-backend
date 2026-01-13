from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import EventPermission

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminWaitlistController(EventAdminBaseController):
    """Event waitlist management endpoints."""

    @route.get(
        "/waitlist",
        url_name="list_waitlist",
        response=PaginatedResponseSchema[schema.WaitlistEntrySchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "user__preferred_name"])
    def list_waitlist(
        self,
        event_id: UUID,
    ) -> QuerySet[models.EventWaitList]:
        """List all users on the event waitlist.

        Shows users waiting for spots to become available, ordered by join time (FIFO).
        Use this to see who is waiting and manage the waitlist.
        """
        event = self.get_one(event_id)
        return models.EventWaitList.objects.select_related("user").filter(event=event).order_by("created_at")

    @route.delete(
        "/waitlist/{waitlist_id}",
        url_name="delete_waitlist_entry",
        response={204: None},
        permissions=[EventPermission("invite_to_event")],
    )
    def delete_waitlist_entry(self, event_id: UUID, waitlist_id: UUID) -> tuple[int, None]:
        """Remove a user from the event waitlist.

        Use this to manually remove someone from the waitlist (e.g., if they requested removal
        or if you want to manage the list manually).
        """
        event = self.get_one(event_id)
        waitlist_entry = get_object_or_404(models.EventWaitList, pk=waitlist_id, event=event)
        waitlist_entry.delete()
        return 204, None
