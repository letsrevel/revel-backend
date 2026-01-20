from uuid import UUID

from django.db.models import QuerySet
from ninja import Query
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth, OptionalAuth
from events import filters, models, schema
from events.service import event_service

from .base import EventPublicBaseController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventPublicDetailsController(EventPublicBaseController):
    """Handles event detail retrieval, resources, and attendee information."""

    @route.get("/{org_slug}/event/{event_slug}", url_name="get_event_by_slug", response=schema.EventDetailSchema)
    def get_event_by_slugs(self, org_slug: str, event_slug: str) -> models.Event:
        """Retrieve event details using human-readable organization and event slugs.

        Use this for clean URLs like /events/tech-meetup/event/monthly-session. Returns 404 if
        the event doesn't exist, or you don't have permission to view it.
        """
        return self.get_one_by_slugs(org_slug, event_slug)

    @route.get("/{uuid:event_id}", url_name="get_event", response=schema.EventDetailSchema)
    def get_event(self, event_id: UUID) -> models.Event:
        """Retrieve full event details by ID.

        Returns comprehensive event information including description, location, times, organization,
        ticket tiers, and visibility settings. Use this to display the event detail page.
        """
        return self.get_one(event_id)

    @route.get(
        "/{uuid:event_id}/attendee-list",
        url_name="event_attendee_list",
        response=PaginatedResponseSchema[schema.AttendeeSchema],
        auth=I18nJWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def get_event_attendees(self, event_id: UUID) -> QuerySet[RevelUser]:
        """Get the list of confirmed attendees for this event.

        Returns users who have RSVPed 'yes' or have active tickets. Visibility is controlled by
        event settings - attendee lists may be hidden from regular attendees. Organization staff
        and event creators always have access.
        """
        event = self.get_one(event_id)
        return event.attendees(self.user()).distinct()

    @route.get(
        "/{uuid:event_id}/resources",
        url_name="list_event_resources",
        response=PaginatedResponseSchema[schema.AdditionalResourceSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_resources(
        self,
        event_id: UUID,
        params: filters.ResourceFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.AdditionalResource]:
        """Get supplementary resources attached to this event.

        Returns resources like documents, links, or media files provided by event organizers.
        Resources may be public or restricted to attendees only. Supports filtering by type
        (file, link, etc.) and text search.
        """
        event = self.get_one(event_id)
        qs = models.AdditionalResource.objects.for_user(self.maybe_user()).filter(events=event).with_related()
        return params.filter(qs).distinct()

    @route.get(
        "/{uuid:event_id}/dietary-summary",
        url_name="event_dietary_summary",
        response=schema.EventDietarySummarySchema,
        auth=I18nJWTAuth(),
    )
    def get_dietary_summary(self, event_id: UUID) -> schema.EventDietarySummarySchema:
        """Get aggregated dietary restrictions and preferences for event attendees.

        Returns de-identified, aggregated dietary information to help with meal planning for events
        and potlucks. Event organizers/staff see all dietary data (public + private). Regular attendees
        only see data marked as public by other attendees. Data includes counts of restrictions/preferences
        and non-empty notes/comments, but no user associations for privacy.
        """
        event = self.get_one(event_id)
        return event_service.get_event_dietary_summary(event, self.user())
