"""Admin interface for referral models."""

import typing as t

from django.contrib import admin
from unfold.admin import ModelAdmin

from accounts.models import Referral, ReferralCode, ReferralPayout


@admin.register(ReferralCode)
class ReferralCodeAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for ReferralCode model (admin-managed, codes are immutable)."""

    list_display = ["user", "code", "is_active", "created_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["user__username", "user__email", "code"]
    autocomplete_fields = ["user"]
    ordering = ["-created_at"]

    fieldsets = [
        (
            "Referral Code",
            {
                "fields": (
                    "user",
                    "code",
                    "is_active",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    def get_readonly_fields(self, request: t.Any, obj: t.Any = None) -> list[str]:
        """User and code are editable on creation but immutable afterwards."""
        base = ["created_at", "updated_at"]
        if obj:
            return base + ["code", "user"]
        return base


@admin.register(Referral)
class ReferralAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for Referral model (system-created only, fully readonly)."""

    list_display = ["referrer", "referred_user", "revenue_share_percent", "created_at"]
    list_filter = ["created_at"]
    search_fields = [
        "referrer__username",
        "referrer__email",
        "referred_user__username",
        "referred_user__email",
        "referral_code__code",
    ]
    readonly_fields = [
        "referral_code",
        "referrer",
        "referred_user",
        "revenue_share_percent",
        "created_at",
        "updated_at",
    ]
    ordering = ["-created_at"]

    fieldsets = [
        (
            "Referral",
            {
                "fields": (
                    "referral_code",
                    "referrer",
                    "referred_user",
                    "revenue_share_percent",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    def has_add_permission(self, request: t.Any) -> bool:
        """Referrals are created by the system during registration."""
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Referrals are immutable."""
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Referrals must not be deleted to preserve the audit trail."""
        return False


@admin.register(ReferralPayout)
class ReferralPayoutAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for ReferralPayout model (system-created only, fully readonly)."""

    list_display = [
        "referral",
        "period_start",
        "payout_amount",
        "currency",
        "status",
        "stripe_transfer_id",
        "created_at",
    ]
    list_filter = ["status", "currency", "period_start"]
    search_fields = [
        "referral__referrer__username",
        "referral__referrer__email",
        "referral__referred_user__username",
        "referral__referred_user__email",
        "stripe_transfer_id",
    ]
    readonly_fields = [
        "referral",
        "period_start",
        "period_end",
        "gross_platform_fees",
        "payout_amount",
        "currency",
        "status",
        "stripe_transfer_id",
        "created_at",
        "updated_at",
    ]
    ordering = ["-period_start"]

    fieldsets = [
        (
            "Payout",
            {
                "fields": (
                    "referral",
                    "period_start",
                    "period_end",
                    "gross_platform_fees",
                    "payout_amount",
                    "currency",
                    "status",
                    "stripe_transfer_id",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    def has_add_permission(self, request: t.Any) -> bool:
        """Payouts are created by the system."""
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Payouts are immutable."""
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Payouts must not be deleted to preserve the audit trail."""
        return False
