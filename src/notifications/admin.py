"""Django admin for notification models."""

import typing as t

from django import forms
from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from unfold.contrib.forms.widgets import WysiwygWidget
from unfold.widgets import CHECKBOX_CLASSES, UnfoldAdminTextInputWidget

from accounts.models import RevelUser
from notifications.context_schemas import SystemAnnouncementContext
from notifications.enums import NotificationType
from notifications.models import Notification, NotificationDelivery, NotificationPreference
from notifications.service.dispatcher import NotificationData, bulk_create_notifications
from notifications.tasks import dispatch_notifications_batch

BATCH_SIZE = 500


class SystemAnnouncementForm(forms.Form):
    """Form for sending a system announcement to all active users."""

    title = forms.CharField(
        max_length=200,
        widget=UnfoldAdminTextInputWidget(),
        help_text="The announcement title shown to users.",
    )
    body = forms.CharField(
        widget=WysiwygWidget(),
        help_text="The announcement body (rich text via Trix editor).",
    )
    url = forms.URLField(
        required=False,
        widget=UnfoldAdminTextInputWidget(),
        help_text="Optional link to a relevant page (e.g. privacy policy).",
    )
    include_guests = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": " ".join(CHECKBOX_CLASSES)}),
        help_text="Include guest users (default: non-guest active users only).",
    )


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

    # --- System Announcement view ---

    def get_urls(self) -> list[t.Any]:
        """Add custom URL for sending system announcements."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "send-announcement/",
                self.admin_site.admin_view(self.send_system_announcement),
                name="notifications_notification_send_announcement",
            ),
        ]
        return custom_urls + urls

    def send_system_announcement(self, request: HttpRequest) -> TemplateResponse | HttpResponseRedirect:
        """Handle system announcement form (GET) and dispatch (POST).

        Requires superuser permission.
        """
        if not request.user.is_superuser:
            return HttpResponseRedirect(reverse("admin:index"))

        form = SystemAnnouncementForm(request.POST or None)

        if request.method == "POST" and form.is_valid():
            title: str = form.cleaned_data["title"]
            body: str = form.cleaned_data["body"]
            url: str = form.cleaned_data.get("url") or ""
            include_guests: bool = form.cleaned_data["include_guests"]

            # Build context
            context: SystemAnnouncementContext = {
                "announcement_title": title,
                "announcement_body": body,
            }
            if url:
                context["policy_url"] = url

            # Query target users
            users = RevelUser.objects.filter(is_active=True)
            if not include_guests:
                users = users.filter(guest=False)

            user_count = users.count()
            if user_count == 0:
                messages.info(request, "No active users found matching the criteria.")
                return HttpResponseRedirect(reverse("admin:notifications_notification_changelist"))

            # Create and dispatch in batches
            total_created = 0
            for batch_start in range(0, user_count, BATCH_SIZE):
                batch_users = users[batch_start : batch_start + BATCH_SIZE]
                notifications_data = [
                    NotificationData(
                        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                        user=user,
                        context=dict(context),
                    )
                    for user in batch_users
                ]
                created = bulk_create_notifications(notifications_data)
                batch_ids = [str(n.id) for n in created]
                dispatch_notifications_batch.delay(batch_ids)
                total_created += len(batch_ids)

            messages.success(
                request,
                f"System announcement sent to {total_created} users.",
            )
            return HttpResponseRedirect(reverse("admin:notifications_notification_changelist"))

        template_context = {
            **self.admin_site.each_context(request),
            "title": "Send System Announcement",
            "form": form,
            "opts": self.model._meta,
        }
        return TemplateResponse(
            request,
            "admin/notifications/send_announcement.html",
            template_context,
        )


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
            {"fields": ("event_reminders_enabled",)},
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
