# src/events/admin/announcement.py
"""Admin class for Announcement model."""

import typing as t

from django.contrib import admin
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    EventLinkMixin,
    OrganizationLinkMixin,
)


@admin.register(models.Announcement)
class AnnouncementAdmin(ModelAdmin, OrganizationLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for Announcement model."""

    def get_queryset(self, request: t.Any) -> t.Any:
        """Optimize queryset to avoid N+1 queries in list view."""
        qs = super().get_queryset(request)
        return qs.select_related("organization", "event").prefetch_related("target_tiers")

    list_display = [
        "title",
        "organization_link",
        "event_link",
        "target_display",
        "status_display",
        "recipient_count",
        "sent_at",
        "created_at",
    ]
    list_filter = ["status", "organization__name", "created_at", "sent_at"]
    search_fields = ["title", "body", "organization__name", "event__name"]
    autocomplete_fields = ["organization", "event", "created_by"]
    readonly_fields = ["status", "sent_at", "recipient_count", "created_at", "updated_at"]
    filter_horizontal = ["target_tiers"]
    date_hierarchy = "created_at"
    fieldsets = [
        (None, {"fields": ["organization", "title", "body", "created_by"]}),
        ("Targeting", {"fields": ["event", "target_all_members", "target_tiers", "target_staff_only"]}),
        ("Settings", {"fields": ["past_visibility"]}),
        ("Status", {"fields": ["status", "sent_at", "recipient_count"]}),
        ("Metadata", {"fields": [("created_at", "updated_at")]}),
    ]

    @admin.display(description="Target")
    def target_display(self, obj: models.Announcement) -> str:
        if obj.event:
            name = obj.event.name[:30]
            return f"Event: {name}..." if len(obj.event.name) > 30 else f"Event: {name}"
        if obj.target_all_members:
            return "All Members"
        if obj.target_staff_only:
            return "Staff Only"
        # Use len() on .all() to leverage prefetched cache instead of .count()
        # which may trigger a separate query in some Django versions
        tier_count = len(obj.target_tiers.all())
        if tier_count:
            return f"{tier_count} tier(s)"
        return "—"

    @admin.display(description="Status")
    def status_display(self, obj: models.Announcement) -> str:
        colors: dict[str, str] = {
            models.Announcement.Status.DRAFT: "orange",
            models.Announcement.Status.SENT: "green",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')
