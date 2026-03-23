# src/events/admin/discount_code.py
"""Admin class for DiscountCode model."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.DiscountCode)
class DiscountCodeAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for DiscountCode model."""

    list_display = [
        "code",
        "organization_link",
        "discount_type",
        "discount_display",
        "usage_display",
        "is_active_display",
        "valid_from",
        "valid_until",
    ]
    list_filter = ["discount_type", "is_active", "organization__name"]
    search_fields = ["code", "organization__name"]
    autocomplete_fields = ["organization"]
    filter_horizontal = ["series", "events", "tiers"]
    readonly_fields = ["times_used", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    fieldsets = [
        (None, {"fields": ["code", "organization", "is_active"]}),
        ("Discount", {"fields": ["discount_type", "discount_value", "currency"]}),
        ("Validity", {"fields": ["valid_from", "valid_until"]}),
        ("Usage Limits", {"fields": ["max_uses", "max_uses_per_user", "times_used", "min_purchase_amount"]}),
        ("Scope (leave empty for org-wide)", {"fields": ["series", "events", "tiers"]}),
        ("Metadata", {"fields": [("created_at", "updated_at")]}),
    ]

    @admin.display(description="Discount")
    def discount_display(self, obj: models.DiscountCode) -> str:
        if obj.discount_type == models.DiscountCode.DiscountType.PERCENTAGE:
            return f"{obj.discount_value}%"
        return f"{obj.discount_value} {obj.currency or ''}"

    @admin.display(description="Usage")
    def usage_display(self, obj: models.DiscountCode) -> str:
        limit = obj.max_uses if obj.max_uses is not None else "\u221e"
        return f"{obj.times_used} / {limit}"

    @admin.display(description="Active", boolean=True)
    def is_active_display(self, obj: models.DiscountCode) -> bool:
        return obj.is_active
