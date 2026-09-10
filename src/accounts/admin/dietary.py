"""Admin interfaces for the dietary models (food items, restrictions, preferences)."""

import typing as t

from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    FoodItem,
    UserDietaryPreference,
)


@admin.register(FoodItem)
class FoodItemAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for FoodItem model."""

    list_display = ["name", "restriction_count", "created_at"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["name"]
    date_hierarchy = "created_at"

    def get_queryset(self, request: t.Any) -> t.Any:
        """Override to annotate restriction count."""
        qs = super().get_queryset(request)
        return qs.annotate(restrictions_count=Count("user_restrictions"))

    @admin.display(description="Restrictions", ordering="restrictions_count")
    def restriction_count(self, obj: FoodItem) -> int:
        return obj.restrictions_count  # type: ignore[no-any-return, attr-defined]

    def has_add_permission(self, request: t.Any) -> bool:
        # Food items are created through dietary restrictions
        return t.cast(bool, request.user.is_superuser)


@admin.register(DietaryRestriction)
class DietaryRestrictionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for DietaryRestriction model."""

    list_display = [
        "__str__",
        "user_link",
        "food_item_link",
        "restriction_type",
        "restriction_type_display",
        "is_public",
        "created_at",
    ]
    list_select_related = ["user", "food_item"]
    list_filter = ["restriction_type", "is_public", "created_at"]
    search_fields = ["user__username", "user__email", "food_item__name", "notes"]
    autocomplete_fields = ["user", "food_item"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "user",
                    "food_item",
                    "restriction_type",
                )
            },
        ),
        (
            "Details",
            {
                "fields": (
                    "notes",
                    "is_public",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "id",
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="User")
    def user_link(self, obj: DietaryRestriction) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Food Item")
    def food_item_link(self, obj: DietaryRestriction) -> str:
        url = reverse("admin:accounts_fooditem_change", args=[obj.food_item.id])
        return format_html('<a href="{}">{}</a>', url, obj.food_item.name)

    @admin.display(description="Severity")
    def restriction_type_display(self, obj: DietaryRestriction) -> str:
        colors = {
            DietaryRestriction.RestrictionType.DISLIKE: "gray",
            DietaryRestriction.RestrictionType.INTOLERANT: "orange",
            DietaryRestriction.RestrictionType.ALLERGY: "red",
            DietaryRestriction.RestrictionType.SEVERE_ALLERGY: "darkred",
        }
        color = colors.get(obj.restriction_type, "gray")  # type: ignore[call-overload]
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_restriction_type_display(),
        )


@admin.register(DietaryPreference)
class DietaryPreferenceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for DietaryPreference model (system-managed)."""

    list_display = ["name", "user_count", "created_at"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["name"]
    date_hierarchy = "created_at"

    def get_queryset(self, request: t.Any) -> t.Any:
        """Override to annotate user count."""
        qs = super().get_queryset(request)
        return qs.annotate(users_count=Count("users"))

    @admin.display(description="Users", ordering="users_count")
    def user_count(self, obj: DietaryPreference) -> int:
        return obj.users_count  # type: ignore[no-any-return, attr-defined]

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        # System-managed preferences should not be easily deleted
        return t.cast(bool, request.user.is_superuser)


@admin.register(UserDietaryPreference)
class UserDietaryPreferenceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for UserDietaryPreference model."""

    list_display = [
        "__str__",
        "user_link",
        "preference_link",
        "comment_preview",
        "is_public",
        "created_at",
    ]
    list_select_related = ["user", "preference"]
    list_filter = ["preference__name", "is_public", "created_at"]
    search_fields = ["user__username", "user__email", "preference__name", "comment"]
    autocomplete_fields = ["user", "preference"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "user",
                    "preference",
                )
            },
        ),
        (
            "Details",
            {
                "fields": (
                    "comment",
                    "is_public",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "id",
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="User")
    def user_link(self, obj: UserDietaryPreference) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Preference")
    def preference_link(self, obj: UserDietaryPreference) -> str:
        url = reverse("admin:accounts_dietarypreference_change", args=[obj.preference.id])
        return format_html('<a href="{}">{}</a>', url, obj.preference.name)

    @admin.display(description="Comment")
    def comment_preview(self, obj: UserDietaryPreference) -> str:
        if obj.comment:
            return obj.comment[:50] + ("..." if len(obj.comment) > 50 else "")
        return "—"
