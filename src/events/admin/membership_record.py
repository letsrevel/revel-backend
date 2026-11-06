"""Admin for the membership register."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.MembershipRecord)
class MembershipRecordAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Legal register of membership periods."""

    list_display = [
        "full_name",
        "organization_link",
        "kind",
        "tier_name",
        "valid_from",
        "valid_until",
        "ended_reason",
        "source",
    ]
    list_select_related = ["organization"]
    list_filter = ["kind", "source", "ended_reason"]
    search_fields = ["full_name", "email", "organization__name", "organization__slug"]
    autocomplete_fields = ["organization", "user"]
    raw_id_fields = ["tier", "ticket", "event", "subscription", "questionnaire_submission", "created_by"]
    readonly_fields = ["created_at", "updated_at"]
