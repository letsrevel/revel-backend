import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja import File
from ninja.files import UploadedFile
from ninja_extra import api_controller, route
from ninja_jwt.authentication import JWTAuth

from common.models import Tag
from common.schema import TagSchema, ValidationErrorResponse
from common.throttling import WriteThrottle
from common.utils import safe_save_uploaded_file
from events import models, schema
from events.service import update_db_instance

from .permissions import EventSeriesPermission
from .user_aware_controller import UserAwareController


@api_controller(
    "/event-series-admin/{series_id}",
    auth=JWTAuth(),
    permissions=[EventSeriesPermission("edit_event_series")],
    tags=["Event Series Admin"],
    throttle=WriteThrottle(),
)
class EventSeriesAdminController(UserAwareController):
    def get_queryset(self) -> QuerySet[models.EventSeries]:
        """Get the queryset of event series visible to the current user."""
        return models.EventSeries.objects.for_user(self.user())

    def get_one(self, series_id: UUID) -> models.EventSeries:
        """Wrapper helper."""
        return t.cast(models.EventSeries, self.get_object_or_exception(self.get_queryset(), pk=series_id))

    @route.put(
        "/",
        url_name="edit_event_series",
        response={200: schema.EventSeriesRetrieveSchema, 400: ValidationErrorResponse},
    )
    def update_event_series(self, series_id: UUID, payload: schema.EventSeriesEditSchema) -> models.EventSeries:
        """Update an existing event series."""
        series = self.get_one(series_id)
        return update_db_instance(series, payload)

    @route.delete(
        "/",
        url_name="delete_event_series",
        response={204: None},
        permissions=[EventSeriesPermission("delete_event_series")],
    )
    def delete_event_series(self, series_id: UUID) -> tuple[int, None]:
        """Delete an event series."""
        series = self.get_one(series_id)
        series.delete()
        return 204, None

    @route.post(
        "/upload-logo",
        url_name="event_series_upload_logo",
        response=schema.EventSeriesRetrieveSchema,
    )
    def upload_logo(self, series_id: UUID, logo: File[UploadedFile]) -> models.EventSeries:
        """Upload logo to event series."""
        series = self.get_one(series_id)
        series = safe_save_uploaded_file(instance=series, field="logo", file=logo, uploader=self.user())
        return series

    @route.post(
        "/upload-cover-art",
        url_name="event_series_upload_cover_art",
        response=schema.EventSeriesRetrieveSchema,
    )
    def upload_cover_art(self, series_id: UUID, cover_art: File[UploadedFile]) -> models.EventSeries:
        """Upload cover art to event series."""
        series = self.get_one(series_id)
        series = safe_save_uploaded_file(instance=series, field="cover_art", file=cover_art, uploader=self.user())
        return series

    @route.post(
        "/tags",
        url_name="add_event_series_tags",
        response=list[TagSchema],
    )
    def add_tags(self, series_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Add one or more tags to the organization."""
        event_series = self.get_one(series_id)
        event_series.tags_manager.add(*payload.tags)
        return event_series.tags_manager.all()

    @route.delete(
        "/tags",
        url_name="clear_event_series_tags",
        response={204: None},
    )
    def clear_tags(self, series_id: UUID) -> tuple[int, None]:
        """Remove one or more tags from the organization."""
        event_series = self.get_one(series_id)
        event_series.tags_manager.clear()
        return 204, None

    @route.post(
        "/tags/remove",
        url_name="remove_event_series_tags",
        response=list[TagSchema],
    )
    def remove_tags(self, series_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Remove one or more tags from the organization."""
        event_series = self.get_one(series_id)
        event_series.tags_manager.remove(*payload.tags)
        return event_series.tags_manager.all()
