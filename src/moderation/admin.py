import typing as t

from django.contrib import admin
from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from moderation.models import ContentReport


@admin.register(ContentReport)
class ContentReportAdmin(ModelAdmin):  # type: ignore[misc]
    """Triage queue for reported content."""

    list_display = ["reason", "status", "source", "snapshot_preview", "target_link", "reporter", "created_at"]
    list_filter = ["status", "source", "reason", "content_type", "created_at"]
    search_fields = ["content_snapshot", "details", "reporter__email"]
    readonly_fields = [
        "content_type",
        "object_id",
        "content_snapshot",
        "reporter",
        "source",
        "created_at",
        "updated_at",
    ]
    autocomplete_fields = ["reporter", "resolved_by"]
    date_hierarchy = "created_at"
    actions = ["mark_dismissed", "mark_actioned"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[ContentReport]:
        """Order open reports first (triage priority), then newest.

        A plain ``ordering`` by the status string would sort 'open' last (after
        'actioned'/'dismissed' alphabetically), burying the reports that need attention.
        """
        qs: QuerySet[ContentReport] = super().get_queryset(request)
        return qs.annotate(
            _status_rank=Case(
                When(status=ContentReport.Status.OPEN, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("_status_rank", "-created_at")

    @admin.display(description="Snapshot")
    def snapshot_preview(self, obj: ContentReport) -> str:
        return (obj.content_snapshot[:60] + "…") if len(obj.content_snapshot) > 60 else (obj.content_snapshot or "—")

    @admin.display(description="Target")
    def target_link(self, obj: ContentReport) -> str:
        target = obj.content_object
        if target is None:
            return format_html("<span>(deleted)</span>")
        url = reverse(f"admin:{obj.content_type.app_label}_{obj.content_type.model}_change", args=[obj.object_id])
        return format_html('<a href="{}">{}</a>', url, str(target))

    def _resolve(self, request: t.Any, queryset: t.Any, status: str) -> None:
        queryset.update(status=status, resolved_by=request.user, resolved_at=timezone.now())

    @admin.action(description="Dismiss selected reports")
    def mark_dismissed(self, request: t.Any, queryset: t.Any) -> None:
        self._resolve(request, queryset, ContentReport.Status.DISMISSED)

    @admin.action(description="Mark selected reports actioned")
    def mark_actioned(self, request: t.Any, queryset: t.Any) -> None:
        self._resolve(request, queryset, ContentReport.Status.ACTIONED)
