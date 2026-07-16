# src/events/admin/series_pass.py
"""Admin classes for SeriesPass, SeriesPassTierLink, and HeldSeriesPass models."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    EventLinkMixin,
    SeriesPassTierLinkInline,
    UserLinkMixin,
)


@admin.register(models.SeriesPass)
class SeriesPassAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin view for SeriesPass (season-ticket product on an EventSeries)."""

    list_display = [
        "name",
        "event_series",
        "price",
        "pro_rata_discount",
        "currency",
        "payment_method",
        "visibility",
        "is_active",
        "quantity_sold",
    ]
    list_select_related = ["event_series"]
    list_filter = ["payment_method", "visibility", "is_active"]
    search_fields = ["name", "event_series__name"]
    autocomplete_fields = ["event_series"]
    inlines = [SeriesPassTierLinkInline]


@admin.register(models.SeriesPassTierLink)
class SeriesPassTierLinkAdmin(ModelAdmin, EventLinkMixin):  # type: ignore[misc]
    """Standalone admin for the SeriesPass<->tier mapping, also managed inline on SeriesPass."""

    list_display = ["series_pass", "event_link", "tier"]
    list_select_related = ["series_pass", "event", "tier"]
    search_fields = ["series_pass__name", "event__name", "tier__name"]
    autocomplete_fields = ["series_pass", "event", "tier"]


@admin.register(models.HeldSeriesPass)
class HeldSeriesPassAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin view for HeldSeriesPass (a user's purchased series pass).

    No ``has_delete_permission`` override: ``series_pass`` is PROTECT and ``tickets``
    is RESTRICT, but Django's admin already catches both ProtectedError and
    RestrictedError in its delete view (see ``django.contrib.admin.utils.NestedObjects``)
    and renders a readable "cannot delete because of related objects" page instead of a
    500 — the same default other admins in this module rely on for FK-guarded models, so
    there's nothing to add here. Teardown of a held pass with materialized tickets goes
    through the service layer, not admin delete.
    """

    list_display = ["id", "series_pass", "user_link", "status", "price_paid", "created_at"]
    list_select_related = ["series_pass", "user"]
    list_filter = ["status"]
    search_fields = ["user__email", "series_pass__name"]
    autocomplete_fields = ["series_pass", "user"]
    readonly_fields = ["id", "stripe_session_id", "price_paid", "created_at", "updated_at"]
    date_hierarchy = "created_at"
