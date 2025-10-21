import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja import Query
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import OptionalAuth
from events import filters, models, schema

from .user_aware_controller import UserAwareController


@api_controller("/event-series", auth=OptionalAuth(), tags=["Event Series"])
class EventSeriesController(UserAwareController):
    def get_queryset(self) -> QuerySet[models.EventSeries]:
        """Get the queryset of event series visible to the current user."""
        return models.EventSeries.objects.for_user(self.maybe_user())

    @route.get(
        "/",
        url_name="list_event_series",
        response=PaginatedResponseSchema[schema.EventSeriesRetrieveSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description", "organization__name"])
    def list_event_series(
        self,
        params: filters.EventSeriesFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventSeries]:
        """Browse event series (recurring event collections) visible to the current user.

        Event series group related recurring events (e.g., "Monthly Tech Meetup"). Results are
        filtered by visibility and permissions. Supports filtering by organization and text search.
        """
        qs = self.get_queryset()
        return params.filter(qs)

    @route.get(
        "/{org_slug}/{series_slug}",
        url_name="get_event_series_by_slugs",
        response=schema.EventSeriesRetrieveSchema,
    )
    def get_event_series_by_slugs(self, org_slug: str, series_slug: str) -> models.EventSeries:
        """Retrieve event series details using human-readable organization and series slugs.

        Use this for clean URLs like /event-series/tech-meetup/monthly-sessions. Returns 404
        if the series doesn't exist or you don't have permission to view it.
        """
        return t.cast(
            models.EventSeries,
            self.get_object_or_exception(self.get_queryset(), slug=series_slug, organization__slug=org_slug),
        )

    @route.get("/{series_id}", url_name="get_event_series", response=schema.EventSeriesRetrieveSchema)
    def get_event_series(self, series_id: UUID) -> models.EventSeries:
        """Retrieve full event series details by ID.

        Returns series information including description, organization, and settings. Use this
        to display the series profile page and list related events.
        """
        return t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))

    @route.get(
        "/{series_id}/resources",
        url_name="list_event_series_resources",
        response=PaginatedResponseSchema[schema.AdditionalResourceSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_resources(
        self,
        series_id: UUID,
        params: filters.ResourceFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.AdditionalResource]:
        """Get resources attached to this event series.

        Returns documents, links, or media files that apply to all events in the series.
        Resources may be public or restricted based on visibility settings. Supports filtering
        by type and text search.
        """
        series = self.get_object_or_exception(self.get_queryset(), pk=series_id)
        qs = models.AdditionalResource.objects.for_user(self.maybe_user()).filter(event_series=series).with_related()
        return params.filter(qs)
