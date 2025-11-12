"""API controller for notification management."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from notifications.models import Notification
from notifications.schema import MarkReadResponseSchema, NotificationSchema, UnreadCountSchema


@api_controller(
    "/api/notifications",
    tags=["Notifications"],
    auth=I18nJWTAuth(),
    throttle=UserDefaultThrottle(),
)
class NotificationController(UserAwareController):
    """API endpoints for in-app notifications."""

    @route.get(
        "",
        response=PageNumberPaginationExtra.get_response_schema(NotificationSchema),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_notifications(
        self,
        unread_only: bool = Query(False, description="Filter to unread notifications only"),  # type: ignore[type-arg]
        notification_type: str | None = Query(None, description="Filter by notification type"),  # type: ignore[type-arg]
    ) -> QuerySet[Notification]:
        """List user's notifications.

        Supports filtering by unread status and notification type.
        """
        qs = Notification.objects.filter(user=self.user())

        if unread_only:
            qs = qs.filter(read_at__isnull=True)

        if notification_type:
            qs = qs.filter(notification_type=notification_type)

        qs = qs.order_by("-created_at")

        return qs

    @route.get("/unread-count", response=UnreadCountSchema)
    def unread_count(self) -> dict[str, int]:
        """Get count of unread notifications for current user."""
        count = Notification.objects.filter(user=self.user(), read_at__isnull=True).count()

        return {"count": count}

    @route.post(
        "/{notification_id}/mark-read",
        response=MarkReadResponseSchema,
        throttle=WriteThrottle(),
    )
    def mark_read(self, notification_id: UUID) -> dict[str, bool]:
        """Mark a notification as read."""
        notification = get_object_or_404(Notification, id=notification_id, user=self.user())

        notification.mark_read()

        return {"success": True}

    @route.post(
        "/{notification_id}/mark-unread",
        response=MarkReadResponseSchema,
        throttle=WriteThrottle(),
    )
    def mark_unread(self, notification_id: UUID) -> dict[str, bool]:
        """Mark a notification as unread."""
        notification = get_object_or_404(Notification, id=notification_id, user=self.user())

        notification.mark_unread()

        return {"success": True}

    @route.post("/mark-all-read", response=MarkReadResponseSchema, throttle=WriteThrottle())
    def mark_all_read(self) -> dict[str, bool]:
        """Mark all user's notifications as read."""
        Notification.objects.filter(user=self.user(), read_at__isnull=True).update(read_at=timezone.now())

        return {"success": True}
