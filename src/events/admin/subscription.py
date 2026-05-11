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
    list_select_related = ["tier", "tier__organization"]
    search_fields = ["name", "tier__name", "tier__organization__name"]
    autocomplete_fields = ["tier"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.MembershipSubscription)
class MembershipSubscriptionAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for MembershipSubscription.

    Lifecycle fields (``status``, ``cancelled_at``, ``current_period_*``) are
    readonly here so admin edits cannot bypass the service-layer state
    machine (see :mod:`events.service.subscription_service`).
    """

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
    list_select_related = ["user", "organization", "plan", "plan__tier"]
    search_fields = ["user__username", "user__email", "organization__name", "plan__name"]
    autocomplete_fields = ["user", "organization", "plan"]
    readonly_fields = [
        "status",
        "current_period_start",
        "current_period_end",
        "cancelled_at",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "current_period_end"


@admin.register(models.MembershipPayment)
class MembershipPaymentAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for MembershipPayment.

    ``status`` and the period fields are readonly to keep refunds and
    period mutation flowing through the service layer.
    """

    list_display = ["__str__", "subscription", "amount", "currency", "status", "period_end", "created_at"]
    list_filter = ["status", "currency", "subscription__organization__name"]
    list_select_related = ["subscription", "subscription__user", "subscription__organization"]
    search_fields = [
        "subscription__user__username",
        "subscription__user__email",
        "subscription__organization__name",
    ]
    autocomplete_fields = ["subscription", "recorded_by"]
    readonly_fields = ["status", "period_start", "period_end", "created_at", "updated_at"]
    date_hierarchy = "created_at"
