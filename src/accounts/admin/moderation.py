"""Admin interfaces for platform moderation: impersonation audit log and global bans."""

import typing as t

from django.contrib import admin
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from accounts.models import GlobalBan, ImpersonationLog, RevelUser


@admin.register(ImpersonationLog)
class ImpersonationLogAdmin(ModelAdmin):  # type: ignore[misc]
    """Read-only admin for impersonation audit logs."""

    list_display = [
        "created_at",
        "admin_user_link",
        "target_user_link",
        "status_display",
        "redeemed_at",
        "ip_address",
    ]
    list_select_related = ["admin_user", "target_user"]
    list_filter = ["created_at", "redeemed_at"]
    search_fields = [
        "admin_user__username",
        "admin_user__email",
        "target_user__username",
        "target_user__email",
        "ip_address",
    ]
    readonly_fields = [
        "id",
        "admin_user",
        "target_user",
        "created_at",
        "ip_address",
        "user_agent",
        "token_jti",
        "redeemed_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            _("Impersonation Details"),
            {
                "fields": (
                    "id",
                    ("admin_user", "target_user"),
                    ("created_at", "redeemed_at"),
                )
            },
        ),
        (
            _("Request Information"),
            {
                "fields": (
                    "ip_address",
                    "user_agent",
                    "token_jti",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description=_("Admin"))
    def admin_user_link(self, obj: ImpersonationLog) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.admin_user.id])
        return format_html('<a href="{}">{}</a>', url, obj.admin_user.email)

    @admin.display(description=_("Target User"))
    def target_user_link(self, obj: ImpersonationLog) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.target_user.id])
        return format_html('<a href="{}">{}</a>', url, obj.target_user.email)

    @admin.display(description=_("Status"))
    def status_display(self, obj: ImpersonationLog) -> str:
        if obj.is_redeemed:
            return format_html(
                '<span class="text-green-600 dark:text-green-400 font-medium">{}</span>',
                _("Redeemed"),
            )
        return format_html(
            '<span class="text-orange-600 dark:text-orange-400 font-medium">{}</span>',
            _("Pending"),
        )

    def has_add_permission(self, request: t.Any) -> bool:
        """Prevent manual creation of audit logs."""
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Prevent modification of audit logs."""
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Prevent deletion of audit logs."""
        return False


# --- Global Ban Admin ---


@admin.register(GlobalBan)
class GlobalBanAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for platform-wide user bans."""

    list_display = [
        "ban_type",
        "value",
        "linked_user",
        "reason_preview",
        "created_by_link",
        "created_at",
    ]
    list_select_related = ["user", "created_by"]
    list_filter = ["ban_type", "created_at"]
    search_fields = ["value", "normalized_value", "user__email", "reason"]
    # ``created_by`` is stamped from the request user in ``save_model`` and must not be
    # editable afterwards, or an editor could rewrite who issued the ban.
    readonly_fields = ["normalized_value", "user", "created_by", "created_at", "updated_at"]

    fieldsets = [
        (
            "Ban Details",
            {
                "fields": (
                    "ban_type",
                    "value",
                    "normalized_value",
                    "reason",
                )
            },
        ),
        (
            "Linked Records",
            {
                "fields": (
                    "user",
                    "created_by",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="User")
    def linked_user(self, obj: GlobalBan) -> str:
        if obj.user:
            url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
            return format_html('<a href="{}">{}</a>', url, obj.user.email)
        return "—"

    @admin.display(description="Reason")
    def reason_preview(self, obj: GlobalBan) -> str:
        if obj.reason:
            return obj.reason[:80] + ("..." if len(obj.reason) > 80 else "")
        return "—"

    @admin.display(description="Created By")
    def created_by_link(self, obj: GlobalBan) -> str:
        if obj.created_by:
            url = reverse("admin:accounts_reveluser_change", args=[obj.created_by.id])
            return format_html('<a href="{}">{}</a>', url, obj.created_by.email)
        return "—"

    def save_model(self, request: HttpRequest, obj: GlobalBan, form: t.Any, change: bool) -> None:
        """Auto-set created_by from the request user on creation."""
        if not change:
            obj.created_by = t.cast(RevelUser, request.user)
        super().save_model(request, obj, form, change)
