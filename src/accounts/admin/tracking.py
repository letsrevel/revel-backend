"""Admin for email-verification reminder tracking."""

import typing as t

from django.contrib import admin
from unfold.admin import ModelAdmin

from accounts.models import EmailVerificationReminderTracking


@admin.register(EmailVerificationReminderTracking)
class EmailVerificationReminderTrackingAdmin(ModelAdmin):  # type: ignore[misc]
    """Read-only admin for the email-verification reminder / deactivation pipeline.

    Lets ops answer "who got a final warning but is still unverified" and
    "when was this account's deactivation email sent" — questions the per-user
    inline can't answer across users.
    """

    list_display = ["user", "last_reminder_sent_at", "final_warning_sent_at", "deactivation_email_sent_at"]
    list_select_related = ["user"]
    list_filter = [
        ("last_reminder_sent_at", admin.EmptyFieldListFilter),
        ("final_warning_sent_at", admin.EmptyFieldListFilter),
        ("deactivation_email_sent_at", admin.EmptyFieldListFilter),
    ]
    search_fields = ["user__username", "user__email"]
    readonly_fields = ["user", "last_reminder_sent_at", "final_warning_sent_at", "deactivation_email_sent_at"]
    ordering = ["-last_reminder_sent_at"]

    def has_add_permission(self, request: t.Any) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False
