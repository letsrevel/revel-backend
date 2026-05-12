"""Admin classes for membership subscription models."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin, UserLinkMixin


@admin.register(models.MembershipSubscriptionPlan)
class MembershipSubscriptionPlanAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for MembershipSubscriptionPlan."""

    list_display = [
        "__str__",
        "tier",
        "price",
        "currency",
        "period_unit",
        "period_count",
        "payment_method",
        "is_active",
    ]
    list_filter = ["is_active", "payment_method", "period_unit", "currency", "tier__organization__name"]
    list_select_related = ["tier", "tier__organization"]
    search_fields = ["name", "tier__name", "tier__organization__name", "stripe_price_id", "stripe_product_id"]
    autocomplete_fields = ["tier"]
    # Stripe IDs are populated by the service layer on save; keep them readonly
    # so admins don't accidentally desync the local row from Stripe.
    readonly_fields = ["stripe_product_id", "stripe_price_id", "created_at", "updated_at"]


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
    search_fields = [
        "user__username",
        "user__email",
        "organization__name",
        "plan__name",
        "stripe_subscription_id",
    ]
    autocomplete_fields = ["user", "organization", "plan"]
    readonly_fields = [
        "status",
        "current_period_start",
        "current_period_end",
        "cancelled_at",
        "stripe_subscription_id",
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
    readonly_fields = [
        "status",
        "period_start",
        "period_end",
        "stripe_invoice_id",
        "stripe_payment_intent_id",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"


@admin.register(models.CustomerProfile)
class CustomerProfileAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for the per-(user, organization) Stripe Customer reference."""

    list_display = ["__str__", "user_link", "organization_link", "stripe_customer_id", "created_at"]
    list_filter = ["organization__name"]
    list_select_related = ["user", "organization"]
    search_fields = ["user__username", "user__email", "organization__name", "stripe_customer_id"]
    autocomplete_fields = ["user", "organization"]
    readonly_fields = ["stripe_customer_id", "created_at", "updated_at"]
