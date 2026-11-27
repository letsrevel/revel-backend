"""Admin classes for membership subscription models."""

import typing as t

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from unfold.admin import ModelAdmin

from common.admin import GuardedHistoryAdmin, ViewOnlyHistoryMixin
from events import models
from events.admin.base import OrganizationLinkMixin, UserLinkMixin


class NoOwnDeleteMixin:
    """Refuse a delete on the admin's own pages, but let a cascade from a user or organization delete pass.

    ``has_delete_permission`` is not overridden on purpose: ``get_deleted_objects`` checks it on the admin of
    every cascaded row, so returning False there would block the delete of a user or organization.
    """

    def get_actions(self, request: HttpRequest) -> t.Any:
        """Remove the bulk delete action."""
        actions = super().get_actions(request)  # type: ignore[misc]
        actions.pop("delete_selected", None)
        return actions

    def delete_view(self, request: HttpRequest, object_id: str, extra_context: t.Any = None) -> HttpResponse:
        """Refuse the delete page and its POST."""
        raise PermissionDenied

    def change_view(
        self, request: HttpRequest, object_id: str, form_url: str = "", extra_context: t.Any = None
    ) -> HttpResponse:
        """Hide the delete button."""
        return super().change_view(  # type: ignore[misc, no-any-return]
            request, object_id, form_url, {**(extra_context or {}), "show_delete": False}
        )


@admin.register(models.MembershipSubscriptionPlan)
class MembershipSubscriptionPlanAdmin(ViewOnlyHistoryMixin, GuardedHistoryAdmin, ModelAdmin):  # type: ignore[misc]
    """Admin for MembershipSubscriptionPlan."""

    list_display = [
        "__str__",
        "tier",
        "price",
        "currency",
        "period_unit",
        "period_count",
        "period_alignment",
        "payment_method",
        "is_active",
    ]
    list_filter = [
        "is_active",
        "payment_method",
        "period_unit",
        "period_alignment",
        "currency",
        "tier__organization__name",
    ]
    list_select_related = ["tier", "tier__organization"]
    search_fields = ["name", "tier__name", "tier__organization__name", "stripe_price_id", "stripe_product_id"]
    autocomplete_fields = ["tier"]
    # Stripe IDs are populated by the service layer on save; keep them readonly
    # so admins don't accidentally desync the local row from Stripe.
    readonly_fields = ["stripe_product_id", "stripe_price_id", "created_at", "updated_at"]

    def get_readonly_fields(self, request: HttpRequest, obj: models.MembershipSubscriptionPlan | None = None) -> t.Any:
        """Calendar alignment and start month are create-only, so lock them on existing plans."""
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields += ["period_alignment", "calendar_start_month"]
        return fields


@admin.register(models.MembershipSubscription)
class MembershipSubscriptionAdmin(
    NoOwnDeleteMixin,
    ViewOnlyHistoryMixin,
    GuardedHistoryAdmin,
    ModelAdmin,  # type: ignore[misc]
    UserLinkMixin,
    OrganizationLinkMixin,
):
    """Admin for MembershipSubscription.

    Lifecycle fields (``status``, ``cancelled_at``, ``current_period_*``) are
    readonly here so admin edits cannot bypass the service-layer state
    machine (see :mod:`events.service.subscription.lifecycle`).
    """

    list_display = [
        "__str__",
        "user_link",
        "organization_link",
        "plan",
        "status",
        "current_period_end",
        "cancel_at_period_end",
        "pending_plan",
    ]
    list_filter = ["status", "cancel_at_period_end", "organization__name"]
    list_select_related = ["user", "organization", "plan", "plan__tier", "pending_plan"]
    search_fields = [
        "user__username",
        "user__email",
        "organization__name",
        "plan__name",
        "stripe_subscription_id",
        "stripe_schedule_id",
    ]
    autocomplete_fields = ["user", "organization", "plan", "pending_plan"]
    readonly_fields = [
        "status",
        "current_period_start",
        "current_period_end",
        "cancelled_at",
        "stripe_subscription_id",
        "stripe_schedule_id",
        "pending_plan",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "current_period_end"

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Subscriptions are created by the subscription service only (it checks plan capacity and sales controls)."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.MembershipSubscription | None = None) -> bool:
        """Existing rows are view-only: a save writes the full row and could overwrite a webhook update."""
        if obj is not None:
            return False
        return bool(super().has_change_permission(request, obj))


@admin.register(models.MembershipPayment)
class MembershipPaymentAdmin(NoOwnDeleteMixin, ViewOnlyHistoryMixin, GuardedHistoryAdmin, ModelAdmin):  # type: ignore[misc]
    """Admin for MembershipPayment.

    ``status`` and the period fields are readonly to keep refunds and
    period mutation flowing through the service layer. The ``platform_fee*``
    group is readonly too: it is written as one consistent set by the fee
    decomposition, and a partial admin edit would silently skew invoicing and
    referral payouts.
    """

    list_display = ["__str__", "subscription", "amount", "currency", "status", "period_end", "created_at"]
    list_filter = ["status", "currency", "subscription__organization__name"]
    list_select_related = ["subscription", "subscription__user", "subscription__organization"]
    search_fields = [
        "subscription__user__username",
        "subscription__user__email",
        "subscription__organization__name",
        "stripe_invoice_id",
        "stripe_payment_intent_id",
    ]
    autocomplete_fields = ["subscription", "recorded_by"]
    readonly_fields = [
        "status",
        "period_start",
        "period_end",
        "stripe_invoice_id",
        "stripe_payment_intent_id",
        "platform_fee",
        "platform_fee_net",
        "platform_fee_vat",
        "platform_fee_vat_rate",
        "platform_fee_reverse_charge",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"


@admin.register(models.CustomerProfile)
class CustomerProfileAdmin(ViewOnlyHistoryMixin, GuardedHistoryAdmin, ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for the per-(user, organization) Stripe Customer reference."""

    list_display = ["__str__", "user_link", "organization_link", "stripe_customer_id", "created_at"]
    list_filter = ["organization__name"]
    list_select_related = ["user", "organization"]
    search_fields = ["user__username", "user__email", "organization__name", "stripe_customer_id"]
    autocomplete_fields = ["user", "organization"]
    readonly_fields = ["stripe_customer_id", "created_at", "updated_at"]
