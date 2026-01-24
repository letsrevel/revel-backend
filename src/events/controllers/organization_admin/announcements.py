"""Organization admin announcements controller."""

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching
from pydantic import UUID4

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models
from events.controllers.permissions import OrganizationPermission
from events.schema.announcement import (
    AnnouncementCreateSchema,
    AnnouncementListSchema,
    AnnouncementSchema,
    AnnouncementUpdateSchema,
    RecipientCountSchema,
)
from events.service import announcement_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin - Announcements"],
    throttle=WriteThrottle(),
)
class OrganizationAdminAnnouncementsController(OrganizationAdminBaseController):
    """Organization announcement management endpoints."""

    @route.get(
        "/announcements",
        url_name="list_announcements",
        response=PaginatedResponseSchema[AnnouncementListSchema],
        permissions=[OrganizationPermission("send_announcements")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["title"])
    def list_announcements(
        self,
        slug: str,
        params: filters.AnnouncementFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Announcement]:
        """List all announcements for this organization.

        Returns paginated list of announcements with filtering and search support.
        Includes both draft and sent announcements.

        **Filtering:**
        - `status`: Filter by draft or sent status
        - `event_id`: Filter by target event
        - `has_event`: Filter by whether announcement targets an event

        **Search:**
        - Search by announcement title
        """
        organization = self.get_one(slug)
        return params.filter(
            models.Announcement.objects.filter(organization=organization)
            .select_related("event")
            .order_by("-created_at")
        ).distinct()

    @route.post(
        "/announcements",
        url_name="create_announcement",
        response={201: AnnouncementSchema},
        permissions=[OrganizationPermission("send_announcements")],
    )
    def create_announcement(
        self,
        slug: str,
        payload: AnnouncementCreateSchema,
    ) -> models.Announcement:
        """Create a new draft announcement.

        Creates an announcement in draft status. Use the send endpoint to dispatch
        notifications to recipients.

        **Targeting Options** (exactly one must be selected):
        - `event_id`: Target event attendees (ticket holders and RSVPs)
        - `target_all_members`: Target all active organization members
        - `target_tier_ids`: Target members of specific membership tiers
        - `target_staff_only`: Target only organization staff

        **Fields:**
        - `title`: Announcement title (max 150 chars)
        - `body`: Announcement body (markdown supported)
        - `past_visibility`: If true, new attendees/members can see the announcement
          after it was sent (default: true)
        """
        organization = self.get_one(slug)
        try:
            announcement = announcement_service.create_announcement(
                organization=organization,
                user=self.user(),
                payload=payload,
            )
        except ValueError as e:
            raise HttpError(422, str(e))
        # Reload with prefetched data for schema
        return models.Announcement.objects.full().get(id=announcement.id)

    @route.get(
        "/announcements/{announcement_id}",
        url_name="get_announcement",
        response=AnnouncementSchema,
        permissions=[OrganizationPermission("send_announcements")],
        throttle=UserDefaultThrottle(),
    )
    def get_announcement(
        self,
        slug: str,
        announcement_id: UUID4,
    ) -> models.Announcement:
        """Get details of a specific announcement.

        Returns full announcement details including targeting configuration,
        status, and recipient count (if sent).
        """
        organization = self.get_one(slug)
        return get_object_or_404(
            models.Announcement.objects.full(),
            id=announcement_id,
            organization=organization,
        )

    @route.put(
        "/announcements/{announcement_id}",
        url_name="update_announcement",
        response=AnnouncementSchema,
        permissions=[OrganizationPermission("send_announcements")],
    )
    def update_announcement(
        self,
        slug: str,
        announcement_id: UUID4,
        payload: AnnouncementUpdateSchema,
    ) -> models.Announcement:
        """Update a draft announcement.

        Only draft announcements can be updated. Sent announcements are immutable.

        All fields are optional - only provided fields will be updated.
        """
        organization = self.get_one(slug)
        announcement = get_object_or_404(
            models.Announcement.objects,
            id=announcement_id,
            organization=organization,
            status=models.Announcement.Status.DRAFT,
        )
        try:
            updated = announcement_service.update_announcement(announcement, payload)
        except ValueError as e:
            raise HttpError(422, str(e))
        # Reload with prefetched data for schema
        return models.Announcement.objects.full().get(id=updated.id)

    @route.delete(
        "/announcements/{announcement_id}",
        url_name="delete_announcement",
        response={204: None},
        permissions=[OrganizationPermission("send_announcements")],
    )
    def delete_announcement(
        self,
        slug: str,
        announcement_id: UUID4,
    ) -> tuple[int, None]:
        """Delete a draft announcement.

        Only draft announcements can be deleted. Sent announcements are preserved
        for audit purposes.
        """
        organization = self.get_one(slug)
        announcement = get_object_or_404(
            models.Announcement.objects,
            id=announcement_id,
            organization=organization,
            status=models.Announcement.Status.DRAFT,
        )
        announcement.delete()
        return 204, None

    @route.post(
        "/announcements/{announcement_id}/send",
        url_name="send_announcement",
        response=AnnouncementSchema,
        permissions=[OrganizationPermission("send_announcements")],
    )
    def send_announcement(
        self,
        slug: str,
        announcement_id: UUID4,
    ) -> models.Announcement:
        """Send a draft announcement to all recipients.

        Creates notifications for all targeted recipients and updates the
        announcement status to sent. The announcement becomes immutable after
        sending.

        Notifications are dispatched asynchronously and respect user notification
        preferences.

        **Returns:**
        Updated announcement with:
        - `status`: "sent"
        - `sent_at`: Timestamp when sent
        - `recipient_count`: Number of notifications created
        """
        organization = self.get_one(slug)
        announcement = get_object_or_404(
            models.Announcement.objects.select_related("organization", "event", "created_by"),
            id=announcement_id,
            organization=organization,
            status=models.Announcement.Status.DRAFT,
        )
        announcement_service.send_announcement(announcement)
        # Reload with prefetched data for schema
        return models.Announcement.objects.full().get(id=announcement.id)

    @route.get(
        "/announcements/{announcement_id}/recipient-count",
        url_name="get_announcement_recipient_count",
        response=RecipientCountSchema,
        permissions=[OrganizationPermission("send_announcements")],
        throttle=UserDefaultThrottle(),
    )
    def get_recipient_count(
        self,
        slug: str,
        announcement_id: UUID4,
    ) -> dict[str, int]:
        """Get the recipient count for an announcement.

        Useful for previewing how many users will receive the announcement
        before sending. Works for both draft and sent announcements.

        For sent announcements, returns the stored recipient_count.
        For drafts, calculates the current recipient count based on targeting.
        """
        organization = self.get_one(slug)
        announcement = get_object_or_404(
            models.Announcement.objects.select_related("event").prefetch_related("target_tiers"),
            id=announcement_id,
            organization=organization,
        )

        if announcement.status == models.Announcement.Status.SENT:
            count = announcement.recipient_count
        else:
            count = announcement_service.get_recipient_count(announcement)

        return {"count": count}
