# src/events/admin/invoice.py
"""Admin classes for PlatformFeeInvoice and PlatformFeeCreditNote models."""

import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from common.signing import get_file_url
from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.PlatformFeeInvoice)
class PlatformFeeInvoiceAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for PlatformFeeInvoice with filtering and PDF download."""

    list_select_related = ["organization"]
    list_display = [
        "invoice_number",
        "organization_link",
        "period_label",
        "fee_display",
        "vat_display",
        "status_display",
        "reverse_charge_display",
        "issued_at",
    ]
    list_filter = ["status", "currency", "reverse_charge", "period_start"]
    search_fields = ["invoice_number", "org_name", "organization__name"]
    readonly_fields = [
        "id",
        "invoice_number",
        "organization",
        "period_start",
        "period_end",
        "fee_gross",
        "fee_net",
        "fee_vat",
        "fee_vat_rate",
        "currency",
        "reverse_charge",
        "org_name",
        "org_vat_id",
        "org_vat_country",
        "org_address",
        "platform_business_name",
        "platform_business_address",
        "platform_vat_id",
        "total_tickets",
        "total_ticket_revenue",
        "issued_at",
        "pdf_link",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "period_start"
    ordering = ["-period_start"]

    fieldsets = [
        (None, {"fields": ["id", "invoice_number", "organization", "status", "issued_at", "pdf_link"]}),
        ("Period", {"fields": ["period_start", "period_end", "currency"]}),
        (
            "Fee Breakdown",
            {"fields": ["fee_net", "fee_vat", "fee_vat_rate", "fee_gross", "reverse_charge"]},
        ),
        (
            "Organization Snapshot",
            {"fields": ["org_name", "org_vat_id", "org_vat_country", "org_address"]},
        ),
        (
            "Platform Snapshot",
            {"fields": ["platform_business_name", "platform_business_address", "platform_vat_id"]},
        ),
        ("Aggregate Stats", {"fields": ["total_tickets", "total_ticket_revenue"]}),
    ]

    @admin.display(description="Period")
    def period_label(self, obj: models.PlatformFeeInvoice) -> str:
        return obj.period_start.strftime("%B %Y")

    @admin.display(description="Fee (gross)")
    def fee_display(self, obj: models.PlatformFeeInvoice) -> str:
        return f"{obj.fee_gross} {obj.currency}"

    @admin.display(description="VAT")
    def vat_display(self, obj: models.PlatformFeeInvoice) -> str:
        if obj.reverse_charge:
            return "RC"
        return f"{obj.fee_vat} ({obj.fee_vat_rate}%)"

    @admin.display(description="Status")
    def status_display(self, obj: models.PlatformFeeInvoice) -> str:
        colors: dict[t.Any, str] = {
            models.PlatformFeeInvoice.InvoiceStatus.DRAFT: "gray",
            models.PlatformFeeInvoice.InvoiceStatus.ISSUED: "blue",
            models.PlatformFeeInvoice.InvoiceStatus.PAID: "green",
            models.PlatformFeeInvoice.InvoiceStatus.CANCELLED: "red",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="RC", boolean=True)
    def reverse_charge_display(self, obj: models.PlatformFeeInvoice) -> bool:
        return obj.reverse_charge

    @admin.display(description="PDF")
    def pdf_link(self, obj: models.PlatformFeeInvoice) -> str:
        if url := get_file_url(obj.pdf_file):
            return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">Download PDF</a>', url)
        return "—"


@admin.register(models.PlatformFeeCreditNote)
class PlatformFeeCreditNoteAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for PlatformFeeCreditNote."""

    list_display = [
        "credit_note_number",
        "invoice_link",
        "fee_display",
        "issued_at",
        "created_at",
    ]
    list_select_related = ["invoice"]
    list_filter = ["issued_at", "created_at"]
    search_fields = ["credit_note_number", "invoice__invoice_number", "invoice__org_name"]
    readonly_fields = [
        "id",
        "credit_note_number",
        "invoice",
        "payment",
        "fee_gross",
        "fee_net",
        "fee_vat",
        "issued_at",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    @admin.display(description="Invoice")
    def invoice_link(self, obj: models.PlatformFeeCreditNote) -> str:
        url = reverse("admin:events_platformfeeinvoice_change", args=[obj.invoice_id])
        return format_html('<a href="{}">{}</a>', url, obj.invoice.invoice_number)

    @admin.display(description="Fee (gross)")
    def fee_display(self, obj: models.PlatformFeeCreditNote) -> str:
        return str(obj.fee_gross)
