import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja import File
from ninja.files import UploadedFile
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.models import Tag
from common.schema import TagSchema, ValidationErrorResponse
from common.throttling import WriteThrottle
from common.thumbnails.service import delete_image_with_derivatives
from common.utils import safe_save_uploaded_file
from events import models, schema
from events.service import update_db_instance

from .permissions import EventSeriesPermission


@api_controller(
    "/event-series-admin/{series_id}",
    auth=I18nJWTAuth(),
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
        """Update event series details (admin only).

        Modify series name, description, or settings. Requires 'edit_event_series' permission
        (organization staff/owners). Changes apply to the series but not individual events.
        """
        series = self.get_one(series_id)
        return update_db_instance(series, payload)

    @route.delete(
        "/",
        url_name="delete_event_series",
        response={204: None},
        permissions=[EventSeriesPermission("delete_event_series")],
    )
    def delete_event_series(self, series_id: UUID) -> tuple[int, None]:
        """Permanently delete an event series (admin only).

        Removes the series. Events in the series are not deleted but become standalone.
        Requires 'delete_event_series' permission (typically organization owners only).
        """
        series = self.get_one(series_id)
        series.delete()
        return 204, None

    @route.post(
        "/upload-logo",
        url_name="event_series_upload_logo",
        response=schema.EventSeriesRetrieveSchema,
    )
    def upload_logo(self, series_id: UUID, logo: File[UploadedFile]) -> models.EventSeries:
        """Upload a logo image for the event series (admin only).

        Replaces the existing logo. File is scanned for malware before saving. Requires
        'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series = safe_save_uploaded_file(instance=series, field="logo", file=logo, uploader=self.user())
        return series

    @route.post(
        "/upload-cover-art",
        url_name="event_series_upload_cover_art",
        response=schema.EventSeriesRetrieveSchema,
    )
    def upload_cover_art(self, series_id: UUID, cover_art: File[UploadedFile]) -> models.EventSeries:
        """Upload cover art/banner image for the event series (admin only).

        Replaces the existing cover art. File is scanned for malware before saving. Requires
        'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        series = safe_save_uploaded_file(instance=series, field="cover_art", file=cover_art, uploader=self.user())
        return series

    @route.delete(
        "/delete-logo",
        url_name="event_series_delete_logo",
        response={204: None},
    )
    def delete_logo(self, series_id: UUID) -> tuple[int, None]:
        """Delete logo and its derivatives from event series (admin only).

        Removes the logo image and thumbnails. Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        delete_image_with_derivatives(series, "logo")
        return 204, None

    @route.delete(
        "/delete-cover-art",
        url_name="event_series_delete_cover_art",
        response={204: None},
    )
    def delete_cover_art(self, series_id: UUID) -> tuple[int, None]:
        """Delete cover art and its derivatives from event series (admin only).

        Removes the cover art image and thumbnails. Requires 'edit_event_series' permission.
        """
        series = self.get_one(series_id)
        delete_image_with_derivatives(series, "cover_art")
        return 204, None

    @route.post(
        "/tags",
        url_name="add_event_series_tags",
        response=list[TagSchema],
    )
    def add_tags(self, series_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Add tags to categorize the event series (admin only).

        Tags help users discover series through filtering and search. Returns the updated tag list.
        Requires 'edit_event_series' permission.
        """
        event_series = self.get_one(series_id)
        event_series.tags_manager.add(*payload.tags)
        return event_series.tags_manager.all()

    @route.delete(
        "/tags",
        url_name="clear_event_series_tags",
        response={204: None},
    )
    def clear_tags(self, series_id: UUID) -> tuple[int, None]:
        """Remove all tags from the event series (admin only).

        Clears all categorization tags. Requires 'edit_event_series' permission.
        """
        event_series = self.get_one(series_id)
        event_series.tags_manager.clear()
        return 204, None

    @route.post(
        "/tags/remove",
        url_name="remove_event_series_tags",
        response=list[TagSchema],
    )
    def remove_tags(self, series_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Remove specific tags from the event series (admin only).

        Removes only the specified tags, keeping others. Returns the updated tag list. Requires
        'edit_event_series' permission.
        """
        event_series = self.get_one(series_id)
        event_series.tags_manager.remove(*payload.tags)
        return event_series.tags_manager.all()
