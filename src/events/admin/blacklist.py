# src/events/admin/blacklist.py
"""Admin classes for Blacklist and WhitelistRequest models."""

import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    OrganizationLinkMixin,
    UserLinkMixin,
)


@admin.register(models.Blacklist)
class BlacklistAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for Blacklist model."""

    list_display = [
        "__str__",
        "organization_link",
        "user_link",
        "email",
        "telegram_username",
        "name_display",
        "created_by_link",
        "created_at",
    ]
    list_filter = ["organization__name", "created_at"]
    search_fields = [
        "email",
        "telegram_username",
        "phone_number",
        "first_name",
        "last_name",
        "preferred_name",
        "user__username",
        "user__email",
    ]
    autocomplete_fields = ["organization", "user", "created_by"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
    fieldsets = [
        (None, {"fields": ["organization", "user", "reason", "created_by"]}),
        ("Hard Identifiers", {"fields": ["email", "telegram_username", "phone_number"]}),
        ("Name Fields (for fuzzy matching)", {"fields": ["first_name", "last_name", "preferred_name"]}),
        ("Metadata", {"fields": [("created_at", "updated_at")]}),
    ]

    @admin.display(description="User")
    def user_link(self, obj: models.Blacklist) -> str | None:
        if not obj.user:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Created By")
    def created_by_link(self, obj: models.Blacklist) -> str | None:
        if not obj.created_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.created_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.created_by.username)

    @admin.display(description="Name")
    def name_display(self, obj: models.Blacklist) -> str:
        parts = filter(None, [obj.first_name, obj.last_name])
        name = " ".join(parts)
        if obj.preferred_name:
            name = f"{name} ({obj.preferred_name})" if name else obj.preferred_name
        return name or "—"


@admin.register(models.WhitelistRequest)
class WhitelistRequestAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for WhitelistRequest model."""

    list_display = [
        "__str__",
        "user_link",
        "organization_link",
        "status_display",
        "matched_count",
        "decided_by_link",
        "created_at",
    ]
    list_filter = ["status", "organization__name", "created_at"]
    search_fields = ["user__username", "user__email", "organization__name", "message"]
    autocomplete_fields = ["organization", "user", "decided_by"]
    readonly_fields = ["created_at", "updated_at", "decided_at"]
    filter_horizontal = ["matched_blacklist_entries"]
    date_hierarchy = "created_at"
    fieldsets = [
        (None, {"fields": ["organization", "user", "message"]}),
        ("Status", {"fields": ["status", "decided_by", "decided_at"]}),
        ("Matched Entries", {"fields": ["matched_blacklist_entries"]}),
        ("Metadata", {"fields": [("created_at", "updated_at")]}),
    ]

    @admin.display(description="Status")
    def status_display(self, obj: models.WhitelistRequest) -> str:
        colors: dict[t.Any, str] = {
            models.WhitelistRequest.Status.PENDING: "orange",
            models.WhitelistRequest.Status.APPROVED: "green",
            models.WhitelistRequest.Status.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Matched")
    def matched_count(self, obj: models.WhitelistRequest) -> int:
        return obj.matched_blacklist_entries.count()

    @admin.display(description="Decided By")
    def decided_by_link(self, obj: models.WhitelistRequest) -> str | None:
        if not obj.decided_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.decided_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.decided_by.username)
