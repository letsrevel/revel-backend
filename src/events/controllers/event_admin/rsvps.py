from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.service import update_db_instance

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminRSVPsController(EventAdminBaseController):
    """Event RSVP management endpoints."""

    @route.get(
        "/rsvps",
        url_name="list_rsvps",
        response=PaginatedResponseSchema[schema.RSVPDetailSchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "user__preferred_name"])
    def list_rsvps(
        self,
        event_id: UUID,
        params: filters.RSVPFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventRSVP]:
        """List all RSVPs for an event.

        Shows all users who have RSVPed to the event with their status.
        Use this to see who is attending, not attending, or maybe attending.
        Supports filtering by status and user_id.
        """
        event = self.get_one(event_id)
        qs = (
            models.EventRSVP.objects.with_user()
            .with_org_membership(event.organization_id)
            .filter(event=event)
            .order_by("-created_at")
        )
        return params.filter(qs).distinct()

    @route.get(
        "/rsvps/{rsvp_id}",
        url_name="get_rsvp",
        response=schema.RSVPDetailSchema,
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    def get_rsvp(self, event_id: UUID, rsvp_id: UUID) -> models.EventRSVP:
        """Get details of a specific RSVP."""
        event = self.get_one(event_id)
        return get_object_or_404(models.EventRSVP.objects.select_related("user"), pk=rsvp_id, event=event)

    @route.post(
        "/rsvps",
        url_name="create_rsvp",
        response=schema.RSVPDetailSchema,
        permissions=[EventPermission("invite_to_event")],
    )
    def create_rsvp(self, event_id: UUID, payload: schema.RSVPCreateSchema) -> models.EventRSVP:
        """Create an RSVP on behalf of a user.

        Use this when a user contacts the organization to RSVP outside the platform
        (e.g., via text, email, or in person).
        """
        event = self.get_one(event_id)

        # Verify user exists
        user = get_object_or_404(RevelUser, pk=payload.user_id)

        # Create or update RSVP (due to unique constraint on event+user)
        rsvp, created = models.EventRSVP.objects.update_or_create(
            event=event, user=user, defaults={"status": payload.status}
        )

        return rsvp

    @route.put(
        "/rsvps/{rsvp_id}",
        url_name="update_rsvp",
        response=schema.RSVPDetailSchema,
        permissions=[EventPermission("invite_to_event")],
    )
    def update_rsvp(self, event_id: UUID, rsvp_id: UUID, payload: schema.RSVPUpdateSchema) -> models.EventRSVP:
        """Update an existing RSVP.

        Use this to change a user's RSVP status when they contact you to update their response.
        """
        event = self.get_one(event_id)
        rsvp = get_object_or_404(models.EventRSVP, pk=rsvp_id, event=event)
        return update_db_instance(rsvp, payload)

    @route.delete(
        "/rsvps/{rsvp_id}",
        url_name="delete_rsvp",
        response={204: None},
        permissions=[EventPermission("invite_to_event")],
    )
    def delete_rsvp(self, event_id: UUID, rsvp_id: UUID) -> tuple[int, None]:
        """Delete an RSVP.

        Use this to remove a user's RSVP entirely from the event.
        Note: This is different from setting status to "no" - it completely removes the RSVP record.
        """
        event = self.get_one(event_id)
        rsvp = get_object_or_404(models.EventRSVP, pk=rsvp_id, event=event)
        rsvp.delete()
        return 204, None
