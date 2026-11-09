"""Admin for private organizer notes."""

import typing as t

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.OrganizationUserNote)
class OrganizationUserNoteAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Private organizer notes about users (personal data: handle with care)."""

    list_display = ["__str__", "organization_link", "user", "record", "updated_by", "updated_at"]
    list_select_related = ["organization", "user", "record", "updated_by"]
    search_fields = ["organization__name", "organization__slug", "user__email", "user__username"]
    autocomplete_fields = ["organization", "user", "record"]
    readonly_fields = ["created_at", "updated_at", "updated_by"]

    def save_model(self, request: HttpRequest, obj: models.OrganizationUserNote, form: t.Any, change: bool) -> None:
        """Record the acting admin as the author."""
        obj.updated_by = request.user  # type: ignore[assignment]
        super().save_model(request, obj, form, change)
