"""Admin classes for membership subscription models."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin, UserLinkMixin


@admin.register(models.MembershipSubscriptionPlan)
class MembershipSubscriptionPlanAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for MembershipSubscriptionPlan."""

    list_display = ["__str__", "tier", "price", "currency", "period_unit", "period_count", "is_active"]
    list_filter = ["is_active", "period_unit", "currency", "tier__organization__name"]
    search_fields = ["name", "tier__name", "tier__organization__name"]
    autocomplete_fields = ["tier"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.MembershipSubscription)
class MembershipSubscriptionAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for MembershipSubscription."""

    list_display = [
        "__str__",
        "user_link",
        "organization_link",
        "plan",
        "status",
        "current_period_end",
        "cancel_at_period_end",
    ]
    list_filter = ["status", "cancel_at_period_end", "organization__name"]
    search_fields = ["user__username", "user__email", "organization__name", "plan__name"]
    autocomplete_fields = ["user", "organization", "plan"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "current_period_end"


@admin.register(models.MembershipPayment)
class MembershipPaymentAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for MembershipPayment."""

    list_display = ["__str__", "subscription", "amount", "currency", "status", "period_end", "created_at"]
    list_filter = ["status", "currency", "subscription__organization__name"]
    search_fields = [
        "subscription__user__username",
        "subscription__user__email",
        "subscription__organization__name",
    ]
    autocomplete_fields = ["subscription", "recorded_by"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
