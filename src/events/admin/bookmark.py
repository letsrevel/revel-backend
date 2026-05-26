"""Admin class for the event bookmark model."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import EventLinkMixin, UserLinkMixin


@admin.register(models.EventBookmark)
class EventBookmarkAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for EventBookmark."""

    list_display = ["__str__", "user_link", "event_link", "created_at"]
    list_filter = ["event__organization"]
    list_select_related = ["user", "event", "event__organization"]
    search_fields = ["user__username", "user__email", "event__name"]
    autocomplete_fields = ["user", "event"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
