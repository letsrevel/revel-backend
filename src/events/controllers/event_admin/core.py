from uuid import UUID

from django.utils.translation import gettext_lazy as _
from ninja import File
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.models import Tag
from common.schema import TagSchema, ValidationErrorResponse
from common.thumbnails.service import delete_image_with_derivatives
from common.throttling import WriteThrottle
from common.utils import safe_save_uploaded_file
from events import models, schema
from events.controllers.permissions import CanDuplicateEvent, EventPermission
from events.service import event_service, update_db_instance

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminCoreController(EventAdminBaseController):
    """Core event admin operations.

    Handles event CRUD, media uploads, status changes, and tags.
    """

    @route.put(
        "",
        url_name="edit_event",
        response={200: schema.EventDetailSchema, 400: ValidationErrorResponse},
        permissions=[EventPermission("edit_event")],
    )
    def update_event(self, event_id: UUID, payload: schema.EventEditSchema) -> models.Event:
        """Update event by ID."""
        event = self.get_one(event_id)
        return update_db_instance(event, payload)

    @route.delete(
        "",
        url_name="delete_event",
        response={204: None},
        permissions=[EventPermission("delete_event")],
    )
    def delete_event(self, event_id: UUID) -> tuple[int, None]:
        """Delete event by ID."""
        event = self.get_one(event_id)
        event.delete()
        return 204, None

    @route.patch(
        "/slug",
        url_name="edit_event_slug",
        response={200: schema.EventDetailSchema},
        permissions=[EventPermission("edit_event")],
    )
    def edit_slug(self, event_id: UUID, payload: schema.EventEditSlugSchema) -> models.Event:
        """Update the event's slug (URL-friendly identifier).

        The slug must be unique within the organization and must be a valid slug format
        (lowercase letters, numbers, and hyphens only).
        """
        event = self.get_one(event_id)

        # Check if slug already exists for this organization
        if (
            models.Event.objects.filter(organization_id=event.organization_id, slug=payload.slug)
            .exclude(pk=event.pk)
            .exists()
        ):
            raise HttpError(400, str(_("An event with this slug already exists in your organization.")))

        event.slug = payload.slug
        event.save(update_fields=["slug"])
        return event

    @route.post(
        "/duplicate",
        url_name="duplicate_event",
        response={200: schema.EventDetailSchema},
        permissions=[CanDuplicateEvent()],
    )
    def duplicate_event(self, event_id: UUID, payload: schema.EventDuplicateSchema) -> models.Event:
        """Create a copy of this event with a new name and start date.

        All date fields are shifted relative to the new start date. The new event
        is created in DRAFT status. Ticket tiers, suggested potluck items, tags,
        questionnaire links, and resource links are copied. User-specific data
        (tickets, RSVPs, invitations, etc.) is NOT copied.

        Requires create_event permission on the event's organization.
        """
        event = self.get_one(event_id)
        return event_service.duplicate_event(
            template_event=event,
            new_name=payload.name,
            new_start=payload.start,
        )

    @route.post(
        "/actions/update-status/{status}",
        url_name="update_event_status",
        permissions=[EventPermission("manage_event")],
        response=schema.EventDetailSchema,
    )
    def update_event_status(self, event_id: UUID, status: models.Event.EventStatus) -> models.Event:
        """Update event status to the specified value.

        Note: Event opening notifications are handled automatically by the post_save signal
        in events/signals.py which triggers when status field is updated.
        """
        event = self.get_one(event_id)
        event.status = status
        event.save(update_fields=["status"])

        return event

    @route.post(
        "/upload-logo",
        url_name="event_upload_logo",
        response=schema.EventDetailSchema,
        permissions=[EventPermission("edit_event")],
    )
    def upload_logo(self, event_id: UUID, logo: File[UploadedFile]) -> models.Event:
        """Upload logo to event."""
        event = self.get_one(event_id)
        event = safe_save_uploaded_file(instance=event, field="logo", file=logo, uploader=self.user())
        return event

    @route.post(
        "/upload-cover-art",
        url_name="event_upload_cover_art",
        response=schema.EventDetailSchema,
        permissions=[EventPermission("edit_event")],
    )
    def upload_cover_art(self, event_id: UUID, cover_art: File[UploadedFile]) -> models.Event:
        """Upload cover art to event."""
        event = self.get_one(event_id)
        event = safe_save_uploaded_file(instance=event, field="cover_art", file=cover_art, uploader=self.user())
        return event

    @route.delete(
        "/delete-logo",
        url_name="event_delete_logo",
        response={204: None},
        permissions=[EventPermission("edit_event")],
    )
    def delete_logo(self, event_id: UUID) -> tuple[int, None]:
        """Delete logo and its derivatives from event."""
        event = self.get_one(event_id)
        delete_image_with_derivatives(event, "logo")
        return 204, None

    @route.delete(
        "/delete-cover-art",
        url_name="event_delete_cover_art",
        response={204: None},
        permissions=[EventPermission("edit_event")],
    )
    def delete_cover_art(self, event_id: UUID) -> tuple[int, None]:
        """Delete cover art and its derivatives from event."""
        event = self.get_one(event_id)
        delete_image_with_derivatives(event, "cover_art")
        return 204, None

    @route.post(
        "/tags",
        url_name="add_event_tags",
        response=list[TagSchema],
        permissions=[EventPermission("edit_event")],
    )
    def add_tags(self, event_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Add one or more tags to the organization."""
        event = self.get_one(event_id)
        event.tags_manager.add(*payload.tags)
        return event.tags_manager.all()

    @route.delete(
        "/tags",
        url_name="clear_event_tags",
        response={204: None},
        permissions=[EventPermission("edit_event")],
    )
    def clear_tags(self, event_id: UUID) -> tuple[int, None]:
        """Remove one or more tags from the organization."""
        event = self.get_one(event_id)
        event.tags_manager.clear()
        return 204, None

    @route.post(
        "/tags/remove",
        url_name="remove_event_tags",
        response=list[TagSchema],
        permissions=[EventPermission("edit_event")],
    )
    def remove_tags(self, event_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Remove one or more tags from the organization."""
        event = self.get_one(event_id)
        event.tags_manager.remove(*payload.tags)
        return event.tags_manager.all()
