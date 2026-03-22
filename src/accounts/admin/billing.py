"""Standalone admin for UserBillingProfile."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from accounts.models import UserBillingProfile


@admin.register(UserBillingProfile)
class UserBillingProfileAdmin(ModelAdmin):  # type: ignore[misc]
    """Standalone admin for browsing all user billing profiles."""

    list_display = [
        "user",
        "billing_name",
        "vat_country_code",
        "vat_id",
        "vat_id_validated",
        "self_billing_agreed",
    ]
    list_filter = ["vat_id_validated", "self_billing_agreed", "vat_country_code"]
    search_fields = ["user__username", "user__email", "billing_name", "vat_id"]
    readonly_fields = [
        "user",
        "billing_name",
        "vat_id",
        "vat_country_code",
        "vat_id_validated",
        "vat_id_validated_at",
        "vies_request_identifier",
        "billing_address",
        "billing_email",
        "self_billing_agreed",
        "created_at",
        "updated_at",
    ]
    ordering = ["-created_at"]
