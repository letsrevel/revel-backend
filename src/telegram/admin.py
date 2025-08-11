# src/storyteller/admin.py

from django.contrib import admin
from unfold.admin import ModelAdmin

from . import models


@admin.register(models.TelegramUser)
class TelegramUserAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin model for the TelegramUser model."""

    list_display = [
        "user",
        "telegram_id",
        "telegram_username",
    ]
    search_fields = ["user__username", "telegram_username", "user__first_name", "user__last_name"]
    ordering = ["-created_at"]
    autocomplete_fields = ["user"]
