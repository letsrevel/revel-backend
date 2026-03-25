# src/events/admin/attendee_invoice.py
"""Admin classes for AttendeeInvoice and AttendeeInvoiceCreditNote models."""

import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from common.signing import get_file_url
from events import models
from events.admin.base import OrganizationLinkMixin


@admin.register(models.AttendeeInvoice)
class AttendeeInvoiceAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for AttendeeInvoice with filtering and PDF download."""

    list_select_related = ["organization", "event", "user"]
    list_display = [
        "invoice_number",
        "organization_link",
        "buyer_name",
        "total_display",
        "vat_display",
        "status_display",
        "reverse_charge_display",
        "issued_at",
    ]
    list_filter = ["status", "currency", "reverse_charge"]
    search_fields = [
        "invoice_number",
        "seller_name",
        "buyer_name",
        "buyer_email",
        "organization__name",
    ]
    readonly_fields = [
        "id",
        "invoice_number",
        "organization",
        "event",
        "user",
        "stripe_session_id",
        "total_gross",
        "total_net",
        "total_vat",
        "vat_rate",
        "currency",
        "reverse_charge",
        "discount_code_text",
        "discount_amount_total",
        "line_items",
        "seller_name",
        "seller_vat_id",
        "seller_vat_country",
        "seller_address",
        "seller_email",
        "buyer_name",
        "buyer_vat_id",
        "buyer_vat_country",
        "buyer_address",
        "buyer_email",
        "issued_at",
        "pdf_link",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            None,
            {"fields": ["id", "invoice_number", "organization", "event", "user", "status", "issued_at", "pdf_link"]},
        ),
        (
            "Amounts",
            {"fields": ["total_net", "total_vat", "vat_rate", "total_gross", "currency", "reverse_charge"]},
        ),
        (
            "Discount",
            {"fields": ["discount_code_text", "discount_amount_total"]},
        ),
        (
            "Seller Snapshot",
            {"fields": ["seller_name", "seller_vat_id", "seller_vat_country", "seller_address", "seller_email"]},
        ),
        (
            "Buyer Snapshot",
            {"fields": ["buyer_name", "buyer_vat_id", "buyer_vat_country", "buyer_address", "buyer_email"]},
        ),
        (
            "Technical",
            {"fields": ["stripe_session_id", "line_items"]},
        ),
    ]

    @admin.display(description="Total")
    def total_display(self, obj: models.AttendeeInvoice) -> str:
        return f"{obj.total_gross} {obj.currency}"

    @admin.display(description="VAT")
    def vat_display(self, obj: models.AttendeeInvoice) -> str:
        if obj.reverse_charge:
            return "RC"
        return f"{obj.total_vat} ({obj.vat_rate}%)"

    @admin.display(description="Status")
    def status_display(self, obj: models.AttendeeInvoice) -> str:
        colors: dict[t.Any, str] = {
            models.AttendeeInvoice.InvoiceStatus.DRAFT: "gray",
            models.AttendeeInvoice.InvoiceStatus.ISSUED: "blue",
            models.AttendeeInvoice.InvoiceStatus.CANCELLED: "red",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="RC", boolean=True)
    def reverse_charge_display(self, obj: models.AttendeeInvoice) -> bool:
        return obj.reverse_charge

    @admin.display(description="PDF")
    def pdf_link(self, obj: models.AttendeeInvoice) -> str:
        if url := get_file_url(obj.pdf_file):
            return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">Download PDF</a>', url)
        return "—"


@admin.register(models.AttendeeInvoiceCreditNote)
class AttendeeInvoiceCreditNoteAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for AttendeeInvoiceCreditNote."""

    list_display = [
        "credit_note_number",
        "invoice_link",
        "amount_display",
        "issued_at",
        "created_at",
    ]
    list_select_related = ["invoice"]
    list_filter = ["issued_at", "created_at"]
    search_fields = ["credit_note_number", "invoice__invoice_number", "invoice__seller_name"]
    readonly_fields = [
        "id",
        "credit_note_number",
        "invoice",
        "amount_gross",
        "amount_net",
        "amount_vat",
        "line_items",
        "issued_at",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    @admin.display(description="Invoice")
    def invoice_link(self, obj: models.AttendeeInvoiceCreditNote) -> str:
        url = reverse("admin:events_attendeeinvoice_change", args=[obj.invoice_id])
        return format_html('<a href="{}">{}</a>', url, obj.invoice.invoice_number)

    @admin.display(description="Amount (gross)")
    def amount_display(self, obj: models.AttendeeInvoiceCreditNote) -> str:
        return str(obj.amount_gross)
