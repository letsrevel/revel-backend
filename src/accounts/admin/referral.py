"""Admin interface for referral models."""

import typing as t

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from unfold.admin import ModelAdmin

from accounts.models import Referral, ReferralCode, ReferralPayout, ReferralPayoutStatement, RevelUser
from accounts.service import referral_payout_service
from common.signing import get_file_url


@admin.register(ReferralCode)
class ReferralCodeAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for ReferralCode model (admin-managed, codes are immutable)."""

    list_display = ["user", "code", "is_active", "created_at"]
    list_select_related = ["user"]
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
    """Admin for Referral model (full CRUD for manual sales-rep attribution).

    ``referrer`` is auto-derived from ``referral_code.user`` in ``Referral.save()``,
    so the form only exposes ``referral_code``, ``referred_user`` and the share %.
    """

    list_display = ["referrer", "referred_user", "revenue_share_percent", "created_at"]
    list_select_related = ["referrer", "referred_user"]
    list_filter = ["created_at"]
    search_fields = [
        "referrer__username",
        "referrer__email",
        "referred_user__username",
        "referred_user__email",
        "referral_code__code",
    ]
    autocomplete_fields = ["referral_code", "referred_user"]
    readonly_fields = [
        "referrer",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
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
    list_select_related = ["referral__referrer", "referral__referred_user"]
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
        "net_platform_fees",
        "rolled_over_amount",
        "payout_amount",
        "currency",
        "status",
        "stripe_transfer_id",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "period_start"
    ordering = ["-period_start"]

    fieldsets = [
        (
            "Payout",
            {
                "fields": (
                    "referral",
                    "period_start",
                    "period_end",
                    "net_platform_fees",
                    "rolled_over_amount",
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

    actions = ["requeue_failed_payouts"]

    def has_add_permission(self, request: t.Any) -> bool:
        """Payouts are created by the system."""
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Payouts are immutable."""
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Payouts must not be deleted to preserve the audit trail."""
        return False

    def has_requeue_permission(self, request: HttpRequest) -> bool:
        """Gate the requeue action on the model's change permission.

        ``has_change_permission`` is hard-False so the change form stays readonly,
        which would also hide the action — hence the underlying Django permission
        is consulted directly instead of via ``permissions=["change"]``.
        """
        return request.user.has_perm("accounts.change_referralpayout")

    @admin.action(description=_("Requeue failed payouts"), permissions=["requeue"])
    def requeue_failed_payouts(self, request: HttpRequest, queryset: QuerySet[ReferralPayout]) -> None:
        """Send FAILED payouts back to CALCULATED so the next disbursement run retries them."""
        requeued = referral_payout_service.requeue_failed_payouts(queryset, actor=t.cast(RevelUser, request.user))
        skipped = queryset.count() - len(requeued)

        if requeued:
            self.message_user(
                request,
                ngettext(
                    "Requeued %d failed payout.",
                    "Requeued %d failed payouts.",
                    len(requeued),
                )
                % len(requeued),
                messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                ngettext(
                    "Skipped %d payout (not in failed status).",
                    "Skipped %d payouts (not in failed status).",
                    skipped,
                )
                % skipped,
                messages.WARNING,
            )


@admin.register(ReferralPayoutStatement)
class ReferralPayoutStatementAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for ReferralPayoutStatement (system-created, readonly)."""

    list_display = [
        "document_number",
        "document_type",
        "referrer_name",
        "amount_display",
        "vat_display",
        "reverse_charge_display",
        "issued_at",
        "pdf_link",
    ]
    list_filter = ["document_type", "currency", "reverse_charge"]
    search_fields = [
        "document_number",
        "referrer_name",
        "referrer_vat_id",
        "payout__referral__referrer__email",
    ]
    readonly_fields = [
        "payout",
        "document_type",
        "document_number",
        "amount_gross",
        "amount_net",
        "amount_vat",
        "vat_rate",
        "currency",
        "reverse_charge",
        "referrer_name",
        "referrer_address",
        "referrer_vat_id",
        "referrer_country",
        "platform_business_name",
        "platform_business_address",
        "platform_vat_id",
        "issued_at",
        "pdf_link",
        "created_at",
        "updated_at",
    ]
    ordering = ["-created_at"]

    fieldsets = [
        (None, {"fields": ["payout", "document_type", "document_number", "issued_at", "pdf_link"]}),
        ("Amount", {"fields": ["amount_net", "amount_vat", "vat_rate", "amount_gross", "currency", "reverse_charge"]}),
        ("Referrer Snapshot", {"fields": ["referrer_name", "referrer_address", "referrer_vat_id", "referrer_country"]}),
        (
            "Platform Snapshot",
            {"fields": ["platform_business_name", "platform_business_address", "platform_vat_id"]},
        ),
    ]

    @admin.display(description="Amount (gross)")
    def amount_display(self, obj: ReferralPayoutStatement) -> str:
        """Display gross amount with currency."""
        return f"{obj.amount_gross} {obj.currency}"

    @admin.display(description="VAT")
    def vat_display(self, obj: ReferralPayoutStatement) -> str:
        """Display VAT amount or reverse charge indicator."""
        if obj.reverse_charge:
            return "RC"
        if obj.amount_vat:
            return f"{obj.amount_vat} ({obj.vat_rate}%)"
        return "—"

    @admin.display(description="RC", boolean=True)
    def reverse_charge_display(self, obj: ReferralPayoutStatement) -> bool:
        """Display reverse charge as boolean icon."""
        return obj.reverse_charge

    @admin.display(description="PDF")
    def pdf_link(self, obj: ReferralPayoutStatement) -> str:
        """Display signed download link for the PDF file."""
        if url := get_file_url(obj.pdf_file):
            return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">Download PDF</a>', url)
        return "—"

    def has_add_permission(self, request: t.Any) -> bool:
        """Statements are created by the system."""
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Statements are immutable."""
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Statements must not be deleted to preserve the audit trail."""
        return False
