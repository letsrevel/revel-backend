# src/events/admin/series_pass.py
"""Admin classes for SeriesPass, SeriesPassTierLink, and HeldSeriesPass models."""

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    CreateOnlyFieldsMixin,
    EventLinkMixin,
    SaveEditedFieldsMixin,
    SeriesPassTierLinkInline,
    UserLinkMixin,
)
from events.admin.subscription import NoOwnDeleteMixin


@admin.register(models.SeriesPass)
class SeriesPassAdmin(SaveEditedFieldsMixin, CreateOnlyFieldsMixin, ModelAdmin):  # type: ignore[misc]
    """Admin view for SeriesPass (season-ticket product on an EventSeries)."""

    create_only_fields = ("event_series",)
    readonly_fields = ["quantity_sold"]  # only the series pass services change the count (#1594)

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
class HeldSeriesPassAdmin(SaveEditedFieldsMixin, NoOwnDeleteMixin, CreateOnlyFieldsMixin, ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin view for HeldSeriesPass (a user's purchased series pass).

    The admin cannot add, move or delete a held pass: only the series pass services create one, change its status
    and give its place on the pass back (#1594, #1619). ``NoOwnDeleteMixin`` refuses the admin's own delete but
    keeps the cascade from a user delete.
    """

    # A held pass never moves to another pass or person, and only the services change its status. The admin cannot
    # add one, so these fields are always disabled; they stay on the form for its unique checks.
    create_only_fields = ("series_pass", "user", "status")

    list_display = ["id", "series_pass", "user_link", "status", "price_paid", "created_at"]
    list_select_related = ["series_pass", "user"]
    list_filter = ["status"]
    search_fields = ["user__email", "holder_email", "series_pass__name"]
    autocomplete_fields = ["series_pass", "user"]
    readonly_fields = [
        "id",
        "holder_name",  # holder_name and holder_email are written only by the account erasure (#1476)
        "holder_email",
        "stripe_session_id",
        "price_paid",
        "attribution",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Held passes are created by the series pass service only: it counts the place on the pass (#1594)."""
        return False
