# src/events/admin/venue.py
"""Admin classes for Venue and related models."""

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    OrganizationLinkMixin,
    VenueLinkMixin,
    VenueSeatInline,
    VenueSectorInline,
)


@admin.register(models.Venue)
class VenueAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for Venue model."""

    list_display = ["name", "slug", "organization_link", "capacity", "city_name"]
    list_filter = ["organization__name", "city__country"]
    search_fields = ["name", "slug", "organization__name", "address"]
    autocomplete_fields = ["organization", "city"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "organization",
                    ("name", "slug"),
                    "description",
                    "capacity",
                )
            },
        ),
        (
            "Location",
            {
                "fields": (
                    "city",
                    "address",
                    "location",
                )
            },
        ),
        (
            "Metadata",
            {"fields": (("created_at", "updated_at"),)},
        ),
    ]

    inlines = [VenueSectorInline]

    @admin.display(description="City")
    def city_name(self, obj: models.Venue) -> str:
        return str(obj.city) if obj.city else "—"


@admin.register(models.VenueSector)
class VenueSectorAdmin(ModelAdmin, VenueLinkMixin):  # type: ignore[misc]
    """Admin for VenueSector model."""

    list_display = ["name", "venue_link", "code", "capacity", "display_order"]
    list_filter = ["venue__organization__name"]
    search_fields = ["name", "code", "venue__name"]
    autocomplete_fields = ["venue"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["venue", "display_order", "name"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "venue",
                    ("name", "code"),
                    "capacity",
                )
            },
        ),
        (
            "Display Configuration",
            {
                "fields": (
                    "metadata",
                    "display_order",
                    "shape",
                )
            },
        ),
        (
            "Metadata",
            {"fields": (("created_at", "updated_at"),)},
        ),
    ]

    inlines = [VenueSeatInline]


@admin.register(models.VenueSeat)
class VenueSeatAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for VenueSeat model."""

    list_display = ["label", "sector_link", "row", "number", "is_accessible", "is_obstructed_view", "is_active"]
    list_filter = ["sector__venue__organization__name", "is_accessible", "is_obstructed_view", "is_active"]
    search_fields = ["label", "row", "sector__name", "sector__venue__name"]
    autocomplete_fields = ["sector"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["sector", "row", "number", "label"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "sector",
                    "label",
                    ("row", "number"),
                )
            },
        ),
        (
            "Position & Properties",
            {
                "fields": (
                    "position",
                    ("is_accessible", "is_obstructed_view"),
                    "is_active",
                )
            },
        ),
        (
            "Metadata",
            {"fields": (("created_at", "updated_at"),)},
        ),
    ]

    @admin.display(description="Sector")
    def sector_link(self, obj: models.VenueSeat) -> str:
        url = reverse("admin:events_venuesector_change", args=[obj.sector.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.sector))
