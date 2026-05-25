"""Admin classes for the organization/event-series follow models."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin, UserLinkMixin


@admin.register(models.OrganizationFollow)
class OrganizationFollowAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for OrganizationFollow."""

    list_display = ["__str__", "user_link", "organization_link", "notify_new_events", "is_archived", "created_at"]
    list_filter = ["is_archived", "is_public", "notify_new_events", "notify_announcements"]
    list_select_related = ["user", "organization"]
    search_fields = ["user__username", "user__email", "organization__name"]
    autocomplete_fields = ["user", "organization"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"


@admin.register(models.EventSeriesFollow)
class EventSeriesFollowAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin for EventSeriesFollow."""

    list_display = ["__str__", "user_link", "event_series", "notify_new_events", "is_archived", "created_at"]
    list_filter = ["is_archived", "is_public", "notify_new_events"]
    list_select_related = ["user", "event_series", "event_series__organization"]
    search_fields = ["user__username", "user__email", "event_series__name", "event_series__organization__name"]
    autocomplete_fields = ["user", "event_series"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
