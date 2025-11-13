from django.db.models import Q
from ninja import FilterSchema

from .enums import NotificationType


class NotificationFilterSchema(FilterSchema):
    unread_only: bool = False
    notification_type: NotificationType | None = None

    def filter_unread_only(self, unread_only: bool) -> Q:
        """Helper to find unread only notifications."""
        if unread_only:
            return Q(read_at__isnull=True)
        return Q()
