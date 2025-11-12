"""Django admin for notification models."""

from django.contrib import admin
from django.utils.html import format_html

from notifications.models import Notification, NotificationDelivery, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for Notification model."""

    list_display = [
        "id",
        "notification_type",
        "user_email",
        "title_short",
        "is_read",
        "created_at",
    ]
    list_filter = [
        "notification_type",
        "read_at",
        "created_at",
    ]
    search_fields = [
        "user__email",
        "user__username",
        "title",
        "body",
    ]
    readonly_fields = [
        "id",
        "created_at",
        "updated_at",
        "notification_type",
        "context",
        "attachments",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = (
        (
            "Basic Information",
            {
                "fields": (
                    "id",
                    "notification_type",
                    "user",
                    "title",
                    "body",
                )
            },
        ),
        (
            "Context & Attachments",
            {
                "fields": (
                    "context",
                    "attachments",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Status",
            {
                "fields": (
                    "read_at",
                    "archived_at",
                )
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def user_email(self, obj: Notification) -> str:
        """Get user email."""
        return obj.user.email

    user_email.short_description = "User"  # type: ignore[attr-defined]

    def title_short(self, obj: Notification) -> str:
        """Get shortened title."""
        if len(obj.title) > 50:
            return obj.title[:50] + "..."
        return obj.title

    title_short.short_description = "Title"  # type: ignore[attr-defined]

    def is_read(self, obj: Notification) -> bool:
        """Check if notification is read."""
        return obj.is_read

    is_read.boolean = True  # type: ignore[attr-defined]
    is_read.short_description = "Read"  # type: ignore[attr-defined]


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for NotificationDelivery model."""

    list_display = [
        "id",
        "notification_type",
        "user_email",
        "channel",
        "status_colored",
        "retry_count",
        "delivered_at",
        "created_at",
    ]
    list_filter = [
        "channel",
        "status",
        "created_at",
    ]
    search_fields = [
        "notification__user__email",
        "notification__user__username",
        "notification__title",
    ]
    readonly_fields = [
        "id",
        "notification",
        "channel",
        "attempted_at",
        "delivered_at",
        "created_at",
        "updated_at",
        "metadata",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = (
        (
            "Delivery Information",
            {
                "fields": (
                    "id",
                    "notification",
                    "channel",
                    "status",
                )
            },
        ),
        (
            "Tracking",
            {
                "fields": (
                    "attempted_at",
                    "delivered_at",
                    "retry_count",
                )
            },
        ),
        (
            "Error Information",
            {
                "fields": (
                    "error_message",
                    "metadata",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def notification_type(self, obj: NotificationDelivery) -> str:
        """Get notification type."""
        return obj.notification.notification_type

    notification_type.short_description = "Type"  # type: ignore[attr-defined]

    def user_email(self, obj: NotificationDelivery) -> str:
        """Get user email."""
        return obj.notification.user.email

    user_email.short_description = "User"  # type: ignore[attr-defined]

    def status_colored(self, obj: NotificationDelivery) -> str:
        """Get colored status."""
        color_map = {
            "pending": "orange",
            "sent": "green",
            "failed": "red",
            "skipped": "gray",
        }
        color = color_map.get(obj.status, "black")
        return format_html('<span style="color: {};">{}</span>', color, obj.status.upper())

    status_colored.short_description = "Status"  # type: ignore[attr-defined]


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for NotificationPreference model."""

    list_display = [
        "user_email",
        "silence_all_notifications",
        "digest_frequency",
        "event_reminders_enabled",
        "channels_display",
    ]
    list_filter = [
        "silence_all_notifications",
        "digest_frequency",
        "event_reminders_enabled",
        "show_me_on_attendee_list",
    ]
    search_fields = [
        "user__email",
        "user__username",
    ]
    readonly_fields = [
        "id",
        "created_at",
        "updated_at",
    ]

    fieldsets = (
        (
            "User",
            {"fields": ("user",)},
        ),
        (
            "Global Settings",
            {
                "fields": (
                    "silence_all_notifications",
                    "enabled_channels",
                )
            },
        ),
        (
            "Digest Settings",
            {
                "fields": (
                    "digest_frequency",
                    "digest_send_time",
                )
            },
        ),
        (
            "Event Settings",
            {
                "fields": (
                    "event_reminders_enabled",
                    "show_me_on_attendee_list",
                    "city",
                )
            },
        ),
        (
            "Advanced",
            {
                "fields": ("notification_type_settings",),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "id",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def user_email(self, obj: NotificationPreference) -> str:
        """Get user email."""
        return obj.user.email

    user_email.short_description = "User"  # type: ignore[attr-defined]

    def channels_display(self, obj: NotificationPreference) -> str:
        """Display enabled channels."""
        if not obj.enabled_channels:
            return "None"
        return ", ".join(obj.enabled_channels)

    channels_display.short_description = "Enabled Channels"  # type: ignore[attr-defined]
