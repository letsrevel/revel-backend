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

from common.authentication import I18nJWTAuth, OptionalAuth
from common.controllers import UserAwareController
from common.throttling import WriteThrottle
from events import filters, models, schema
from events.service import follow_service


@api_controller("/event-series", auth=OptionalAuth(), tags=["Event Series"])
class EventSeriesController(UserAwareController):
    def get_queryset(self, full: bool = True) -> QuerySet[models.EventSeries]:
        """Get the queryset of event series visible to the current user."""
        if not full:
            return models.EventSeries.objects.for_user(self.maybe_user())
        return models.EventSeries.objects.full().for_user(self.maybe_user())

    @route.get(
        "/",
        url_name="list_event_series",
        response=PaginatedResponseSchema[schema.EventSeriesInListSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description", "organization__name", "tags__tag__name"])
    def list_event_series(
        self,
        params: filters.EventSeriesFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventSeries]:
        """Browse event series (recurring event collections) visible to the current user.

        Event series group related recurring events (e.g., "Monthly Tech Meetup"). Results are
        filtered by visibility and permissions. Supports filtering by organization and text search.
        """
        qs = self.get_queryset()
        return params.filter(qs).distinct()

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
        return params.filter(qs).distinct()

    @route.get(
        "/{series_id}/follow",
        url_name="get_event_series_follow_status",
        response=schema.EventSeriesFollowStatusSchema,
        auth=I18nJWTAuth(),
    )
    def get_follow_status(self, series_id: UUID) -> schema.EventSeriesFollowStatusSchema:
        """Check if the current user is following this event series.

        Returns whether the user is following and the follow details if applicable.
        """
        event_series = t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))
        is_following = follow_service.is_following_event_series(self.user(), event_series)

        if is_following:
            follow = models.EventSeriesFollow.objects.select_related("event_series", "event_series__organization").get(
                user=self.user(), event_series=event_series, is_archived=False
            )
            return schema.EventSeriesFollowStatusSchema(
                is_following=True,
                follow=schema.EventSeriesFollowSchema.from_model(follow),
            )

        return schema.EventSeriesFollowStatusSchema(is_following=False, follow=None)

    @route.post(
        "/{series_id}/follow",
        url_name="follow_event_series",
        response={201: schema.EventSeriesFollowSchema},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def follow_event_series(
        self, series_id: UUID, payload: schema.EventSeriesFollowCreateSchema
    ) -> tuple[int, schema.EventSeriesFollowSchema]:
        """Follow an event series to receive notifications when new events are added.

        Creates a follow relationship with the event series. You'll receive notifications
        when new events are added to this series.

        **Parameters:**
        - `notify_new_events`: Whether to receive notifications when new events are added

        **Returns:**
        - 201: The created follow relationship

        **Error Cases:**
        - 400: Already following this series
        - 404: Event series not found or not visible
        """
        event_series = t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))
        follow = follow_service.follow_event_series(
            self.user(),
            event_series,
            notify_new_events=payload.notify_new_events,
        )
        return 201, schema.EventSeriesFollowSchema.from_model(follow)

    @route.patch(
        "/{series_id}/follow",
        url_name="update_event_series_follow",
        response=schema.EventSeriesFollowSchema,
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def update_event_series_follow(
        self, series_id: UUID, payload: schema.EventSeriesFollowUpdateSchema
    ) -> schema.EventSeriesFollowSchema:
        """Update notification preferences for an event series you're following.

        Allows you to toggle notification preferences without unfollowing.

        **Parameters:**
        - `notify_new_events`: Whether to receive new event notifications

        **Returns:**
        - The updated follow relationship

        **Error Cases:**
        - 400: Not following this series
        """
        event_series = t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))
        follow = follow_service.update_event_series_follow_preferences(
            self.user(),
            event_series,
            notify_new_events=payload.notify_new_events,
        )
        return schema.EventSeriesFollowSchema.from_model(follow)

    @route.delete(
        "/{series_id}/follow",
        url_name="unfollow_event_series",
        response={204: None},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def unfollow_event_series(self, series_id: UUID) -> tuple[int, None]:
        """Unfollow an event series to stop receiving notifications.

        Removes the follow relationship. Your follow history is preserved internally
        but you'll no longer receive notifications from this series.

        **Returns:**
        - 204: Successfully unfollowed

        **Error Cases:**
        - 400: Not following this series
        """
        event_series = t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))
        follow_service.unfollow_event_series(self.user(), event_series)
        return 204, None

    # NOTE: This catch-all slug route must be LAST to avoid matching UUID-based routes
    # like /{series_id}/follow. In django-ninja-extra 0.31.0+, route order matters.
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
