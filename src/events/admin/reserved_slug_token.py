"""Admin for ReservedSlugToken."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models


@admin.register(models.ReservedSlugToken)
class ReservedSlugTokenAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for the soft reserved-token list."""

    list_display = ("token", "reason", "created_at")
    search_fields = ("token", "reason")
    ordering = ("token",)
