"""Django admin for notification models."""

import typing as t

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, QuerySet
from django.http import HttpRequest, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.forms.widgets import WysiwygWidget
from unfold.widgets import CHECKBOX_CLASSES, UnfoldAdminTextInputWidget

from notifications.models import EmailSuppression, Notification, NotificationDelivery, NotificationPreference
from notifications.service import system_announcement


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


class NotificationDeliveryInline(TabularInline):  # type: ignore[misc]
    """Read-only inline of per-channel deliveries for a notification."""

    model = NotificationDelivery
    extra = 0
    can_delete = False
    readonly_fields = ["channel", "status", "retry_count", "delivered_at", "error_message"]
    fields = ["channel", "status", "retry_count", "delivered_at", "error_message"]

    def has_add_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        return False


@admin.register(Notification)
class NotificationAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Notification model."""

    inlines = [NotificationDeliveryInline]

    list_display = [
        "id",
        "notification_type",
        "user_email",
        "title_short",
        "is_read",
        "created_at",
    ]
    list_select_related = ["user"]
    list_per_page = 50
    show_full_result_count = False
    autocomplete_fields = ["user"]
    list_filter = [
        "notification_type",
        ("read_at", admin.EmptyFieldListFilter),
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
        urls: list[t.Any] = super().get_urls()
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
            context = system_announcement.build_context(
                title=form.cleaned_data["title"],
                body=form.cleaned_data["body"],
                url=form.cleaned_data.get("url") or "",
            )
            recipients = system_announcement.get_recipients(
                include_guests=form.cleaned_data["include_guests"],
                exclude_user=request.user,
            )
            if not recipients.exists():
                messages.info(request, "No active users found matching the criteria.")
                return HttpResponseRedirect(reverse("admin:notifications_notification_changelist"))

            total_created = system_announcement.send(context, recipients)
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
class NotificationDeliveryAdmin(ModelAdmin):  # type: ignore[misc]
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
    list_select_related = ["notification", "notification__user"]
    list_per_page = 50
    show_full_result_count = False
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
        "status",
        "retry_count",
        "error_message",
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
class NotificationPreferenceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for NotificationPreference model."""

    list_display = [
        "user_email",
        "silence_all_notifications",
        "digest_frequency",
        "event_reminders_enabled",
        "channels_display",
        "muted_org_count",
    ]
    list_select_related = ["user"]
    autocomplete_fields = ["user", "muted_organizations"]
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
            "Organization mutes",
            {"fields": ("muted_organizations",)},
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

    def get_queryset(self, request: HttpRequest) -> QuerySet[NotificationPreference]:
        """Annotate the org-mute count for the list column."""
        qs: QuerySet[NotificationPreference] = super().get_queryset(request)
        return qs.annotate(_muted_org_count=Count("muted_organizations"))

    @admin.display(description="Muted orgs", ordering="_muted_org_count")
    def muted_org_count(self, obj: NotificationPreference) -> int:
        """Number of organizations whose announcements this user muted."""
        return obj._muted_org_count  # type: ignore[attr-defined,no-any-return]


class EmailSuppressionAddForm(forms.ModelForm):  # type: ignore[type-arg]
    """Admin form that lets an existing address through, so ``suppress()`` can upsert it by rank."""

    class Meta:
        model = EmailSuppression
        fields = ["email", "reason", "organization", "detail"]

    def validate_unique(self) -> None:
        """Skip the unique-email check; ``suppress()`` upserts (the DB constraint still holds)."""


@admin.register(EmailSuppression)
class EmailSuppressionAdmin(ModelAdmin):  # type: ignore[misc]
    """Addresses Revel won't email. Deleting a row clears the suppression.

    Per-org complaint count (abuse triage): filter reason = "Spam complaint" + organization.
    """

    list_display = ["email", "reason", "source", "organization", "created_at", "updated_at"]
    list_select_related = ["organization"]
    list_filter = ["reason", "source", ("organization", admin.RelatedOnlyFieldListFilter)]
    search_fields = ["email"]
    autocomplete_fields = ["organization"]
    form = EmailSuppressionAddForm
    readonly_fields = ["created_at", "updated_at"]
    fields = ["email", "reason", "organization", "detail", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def has_change_permission(self, request: HttpRequest, obj: EmailSuppression | None = None) -> bool:
        """Rows are immutable here: edits would bypass normalization and the rank rules."""
        return False

    def save_model(self, request: HttpRequest, obj: EmailSuppression, form: t.Any, change: bool) -> None:
        """Route manual additions through ``suppress()`` (normalized address, rank-aware upsert)."""
        from notifications.service.email_policy import suppress

        row = suppress(
            obj.email,
            EmailSuppression.Reason(obj.reason),
            EmailSuppression.Source.ADMIN,
            organization_id=obj.organization_id,
            detail=obj.detail,
        )
        obj.pk = row.pk
        obj._state.adding = False
