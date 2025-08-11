# API admin interfaces for error monitoring and debugging.

import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline

from . import models


class ErrorOccurrenceInline(TabularInline):  # type: ignore[misc]
    """Inline for Error Occurrences."""

    model = models.ErrorOccurrence
    extra = 0
    can_delete = False
    readonly_fields = ["timestamp"]
    fields = ["timestamp"]
    ordering = ["-timestamp"]


@admin.register(models.Error)
class ErrorAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for API Error signatures with debugging information."""

    list_display = [
        "path_short",
        "server_version",
        "occurrence_count",
        "first_seen",
        "last_seen",
        "issue_status",
        "issue_link",
    ]
    list_filter = ["server_version", "issue_solved", "created_at"]
    search_fields = ["path", "traceback", "md5"]
    readonly_fields = [
        "md5",
        "created_at",
        "occurrence_count",
        "traceback_display",
        "payload_display",
        "json_payload_display",
        "request_metadata_display",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = (
        (
            "Error Information",
            {
                "fields": (
                    "md5",
                    "path",
                    "server_version",
                    ("created_at", "occurrence_count"),
                    ("issue_url", "issue_solved"),
                )
            },
        ),
        (
            "Debug Information",
            {
                "fields": (
                    "traceback_display",
                    "request_metadata_display",
                ),
                "classes": ["collapse"],
            },
        ),
        (
            "Payload Data",
            {
                "fields": (
                    "payload_display",
                    "json_payload_display",
                ),
                "classes": ["collapse"],
            },
        ),
    )

    inlines = [ErrorOccurrenceInline]

    @admin.display(description="Path")
    def path_short(self, obj: models.Error) -> str:
        """Show shortened path for better display."""
        path = obj.path
        if len(path) > 60:
            return f"...{path[-57:]}"
        return path

    @admin.display(description="Occurrences")
    def occurrence_count(self, obj: models.Error) -> int:
        """Count of error occurrences."""
        return obj.erroroccurrence_set.count()

    @admin.display(description="First Seen")
    def first_seen(self, obj: models.Error) -> str:
        """Show when error was first seen."""
        return obj.created_at.strftime("%Y-%m-%d %H:%M")

    @admin.display(description="Last Seen")
    def last_seen(self, obj: models.Error) -> str:
        """Show when error was last seen."""
        last_occurrence = obj.erroroccurrence_set.order_by("-timestamp").first()
        if last_occurrence:
            return last_occurrence.timestamp.strftime("%Y-%m-%d %H:%M")
        return obj.created_at.strftime("%Y-%m-%d %H:%M")

    @admin.display(description="Status")
    def issue_status(self, obj: models.Error) -> str:
        """Show issue resolution status."""
        if obj.issue_solved:
            return mark_safe('<span style="color: green;">Resolved</span>')
        elif obj.issue_url:
            return mark_safe('<span style="color: orange;">Tracked</span>')
        else:
            return mark_safe('<span style="color: red;">Open</span>')

    @admin.display(description="Issue")
    def issue_link(self, obj: models.Error) -> str:
        """Show link to issue if available."""
        if obj.issue_url:
            return format_html('<a href="{}" target="_blank">View Issue</a>', obj.issue_url)
        return "—"

    def traceback_display(self, obj: models.Error) -> str:
        """Display formatted traceback."""
        return mark_safe(
            f'<pre style="background: #f8f9fa; padding: 10px; border-radius: 4px; '
            f'font-size: 12px;">{obj.traceback}</pre>'
        )

    traceback_display.short_description = "Traceback"  # type: ignore[attr-defined]

    def payload_display(self, obj: models.Error) -> str:
        """Display payload data if available."""
        if not obj.payload:
            return "—"
        try:
            # Try to decode as text
            payload_text = obj.payload.decode("utf-8", errors="replace")  # type: ignore[union-attr]
            return mark_safe(
                f'<pre style="background: #f8f9fa; padding: 10px; border-radius: 4px; '
                f'font-size: 12px;">{payload_text[:1000]}...</pre>'
            )
        except Exception:
            return f"Binary payload ({len(obj.payload)} bytes)"

    payload_display.short_description = "Payload"  # type: ignore[attr-defined]

    def json_payload_display(self, obj: models.Error) -> str:
        """Display JSON payload if available."""
        if not obj.json_payload:
            return "—"
        import json

        pretty_json = json.dumps(obj.json_payload, indent=2)
        return mark_safe(
            f'<pre style="background: #f8f9fa; padding: 10px; border-radius: 4px; font-size: 12px;">{pretty_json}</pre>'
        )

    json_payload_display.short_description = "JSON Payload"  # type: ignore[attr-defined]

    def request_metadata_display(self, obj: models.Error) -> str:
        """Display request metadata if available."""
        if not obj.request_metadata:
            return "—"
        import json

        pretty_json = json.dumps(obj.request_metadata, indent=2)
        return mark_safe(
            f'<pre style="background: #f8f9fa; padding: 10px; border-radius: 4px; font-size: 12px;">{pretty_json}</pre>'
        )

    request_metadata_display.short_description = "Request Metadata"  # type: ignore[attr-defined]

    def has_add_permission(self, request: t.Any) -> bool:
        return False


@admin.register(models.ErrorOccurrence)
class ErrorOccurrenceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for individual Error Occurrences."""

    list_display = ["error_link", "path_short", "timestamp", "server_version"]
    list_filter = ["signature__server_version", "timestamp"]
    search_fields = ["signature__path", "signature__md5"]
    readonly_fields = ["signature", "timestamp"]
    autocomplete_fields = ["signature"]
    date_hierarchy = "timestamp"
    ordering = ["-timestamp"]

    @admin.display(description="Error Signature")
    def error_link(self, obj: models.ErrorOccurrence) -> str:
        """Link to the error signature."""
        url = reverse("admin:api_error_change", args=[obj.signature.id])
        return format_html('<a href="{}">{}</a>', url, obj.signature.md5[:8])

    @admin.display(description="Path")
    def path_short(self, obj: models.ErrorOccurrence) -> str:
        """Show shortened path for better display."""
        path = obj.signature.path
        if len(path) > 50:
            return f"...{path[-47:]}"
        return path

    @admin.display(description="Version")
    def server_version(self, obj: models.ErrorOccurrence) -> str:
        """Show server version from the error."""
        return obj.signature.server_version

    def has_add_permission(self, request: t.Any) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False
