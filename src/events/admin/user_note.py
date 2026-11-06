"""Admin for private organizer notes."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.OrganizationUserNote)
class OrganizationUserNoteAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Private organizer notes about users (personal data: handle with care)."""

    list_display = ["__str__", "organization_link", "user", "updated_by", "updated_at"]
    list_select_related = ["organization", "user", "updated_by"]
    search_fields = ["organization__name", "organization__slug", "user__email", "user__username"]
    autocomplete_fields = ["organization", "user", "updated_by"]
    readonly_fields = ["created_at", "updated_at"]
