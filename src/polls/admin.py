"""Django admin for the polls app."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from polls.models import Poll


@admin.register(Poll)
class PollAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for the Poll model."""

    list_display = (
        "id",
        "organization",
        "event",
        "status",
        "vote_visibility",
        "result_visibility",
        "closes_at",
    )
    list_filter = ("status", "vote_visibility", "result_visibility", "organization")
    search_fields = ("id", "questionnaire__name", "organization__name")
    autocomplete_fields = ("organization", "event", "questionnaire")
    readonly_fields = ("created_at", "updated_at", "opened_at", "closed_at")
