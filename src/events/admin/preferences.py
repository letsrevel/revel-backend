# src/events/admin/preferences.py
"""Admin classes for user preferences and visibility models."""

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from events import models


@admin.register(models.GeneralUserPreferences)
class GeneralUserPreferencesAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for GeneralUserPreferences."""

    list_display = [
        "__str__",
        "user_link",
        "city_link",
        "show_me_on_attendee_list",
    ]
    list_filter = ["show_me_on_attendee_list", "city__country"]
    search_fields = ["user__username", "user__email", "city__name"]
    autocomplete_fields = ["user", "city"]

    @admin.display(description="User")
    def user_link(self, obj: models.GeneralUserPreferences) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="City")
    def city_link(self, obj: models.GeneralUserPreferences) -> str | None:
        if not obj.city:
            return "—"
        url = reverse("admin:geo_city_change", args=[obj.city.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.city))


@admin.register(models.AttendeeVisibilityFlag)
class AttendeeVisibilityFlagAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for AttendeeVisibilityFlag model."""

    list_display = ["__str__", "user_link", "target_link", "event_link", "is_visible"]
    list_filter = ["is_visible", "event__organization__name"]
    search_fields = ["user__username", "target__username", "event__name"]
    autocomplete_fields = ["user", "target", "event"]

    @admin.display(description="Viewer")
    def user_link(self, obj: models.AttendeeVisibilityFlag) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Target User")
    def target_link(self, obj: models.AttendeeVisibilityFlag) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.target.id])
        return format_html('<a href="{}">{}</a>', url, obj.target.username)

    @admin.display(description="Event")
    def event_link(self, obj: models.AttendeeVisibilityFlag) -> str:
        url = reverse("admin:events_event_change", args=[obj.event.id])
        return format_html('<a href="{}">{}</a>', url, obj.event.name)
