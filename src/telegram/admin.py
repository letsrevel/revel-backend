# src/telegram/admin.py

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from . import models


@admin.register(models.TelegramUser)
class TelegramUserAdmin(ModelAdmin):  # type: ignore[misc]
    """Enhanced admin for TelegramUser with status monitoring."""

    list_display = [
        "__str__",
        "user_link",
        "telegram_id",
        "telegram_username",
        "status_display",
        "was_welcomed",
        "created_at",
    ]
    list_filter = ["blocked_by_user", "user_is_deactivated", "was_welcomed", "user__is_active", "created_at"]
    search_fields = [
        "user__username",
        "user__email",
        "telegram_username",
        "user__first_name",
        "user__last_name",
        "telegram_id",
    ]
    readonly_fields = ["telegram_id", "created_at", "updated_at"]
    ordering = ["-created_at"]
    autocomplete_fields = ["user"]
    date_hierarchy = "created_at"

    def user_link(self, obj: models.TelegramUser) -> str:
        if not obj.user:
            return mark_safe('<span style="color: gray;">Not linked</span>')
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    user_link.short_description = "User"  # type: ignore[attr-defined]
    user_link.admin_order_field = "user__username"  # type: ignore[attr-defined]

    @admin.display(description="Status")
    def status_display(self, obj: models.TelegramUser) -> str:
        if obj.blocked_by_user:
            return mark_safe('<span style="color: red;">Blocked by User</span>')
        elif obj.user_is_deactivated:
            return mark_safe('<span style="color: orange;">User Deactivated</span>')
        elif obj.user and not obj.user.is_active:
            return mark_safe('<span style="color: red;">User Inactive</span>')
        else:
            return mark_safe('<span style="color: green;">Active</span>')
