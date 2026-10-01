"""Admin for the org setup nudge log."""

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.OrganizationNudge)
class OrganizationNudgeAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Read-only log of setup nudges sent to org owners.

    Deleting a row is allowed on purpose: it re-arms that trigger's cap for the org.
    """

    list_display = ["__str__", "organization_link", "trigger", "sequence", "episode_key", "created_at"]
    list_filter = ["trigger", "sequence"]
    list_select_related = ["organization"]
    search_fields = ["organization__name", "organization__slug", "organization__owner__email"]
    readonly_fields = [
        "organization",
        "trigger",
        "episode_key",
        "sequence",
        "target_event",
        "notification",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Rows are written by the nudge service only."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.OrganizationNudge | None = None) -> bool:
        """The log records what was sent; it is not editable."""
        return False
