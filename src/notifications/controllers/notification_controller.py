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
from notifications.filters import NotificationFilterSchema
from notifications.models import Notification
from notifications.schema import NotificationSchema, UnreadCountSchema


@api_controller(
    "/notifications",
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
        params: NotificationFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[Notification]:
        """List user's notifications.

        Supports filtering by unread status and notification type.
        """
        qs = Notification.objects.filter(user=self.user()).order_by("-created_at")
        return params.filter(qs)

    @route.get("/unread-count", response=UnreadCountSchema)
    def unread_count(self) -> dict[str, int]:
        """Get count of unread notifications for current user."""
        count = Notification.objects.filter(user=self.user(), read_at__isnull=True).count()

        return {"count": count}

    @route.post(
        "/{notification_id}/mark-read",
        throttle=WriteThrottle(),
    )
    def mark_read(self, notification_id: UUID) -> None:
        """Mark a notification as read."""
        notification = get_object_or_404(Notification, id=notification_id, user=self.user())
        notification.mark_read()

    @route.post(
        "/{notification_id}/mark-unread",
        throttle=WriteThrottle(),
    )
    def mark_unread(self, notification_id: UUID) -> None:
        """Mark a notification as unread."""
        notification = get_object_or_404(Notification, id=notification_id, user=self.user())
        notification.mark_unread()

    @route.post("/mark-all-read", throttle=WriteThrottle())
    def mark_all_read(self) -> None:
        """Mark all user's notifications as read."""
        Notification.objects.filter(user=self.user(), read_at__isnull=True).update(read_at=timezone.now())
