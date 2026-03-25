"""Invoice, credit note, and attendee invoicing schemas."""

import datetime
import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime

from events.models.attendee_invoice import AttendeeInvoice
from events.models.invoice import PlatformFeeInvoice
from events.models.organization import Organization


class PlatformFeeInvoiceSchema(Schema):
    """Schema for platform fee invoice list/detail responses."""

    id: UUID
    invoice_number: str
    period_start: datetime.date
    period_end: datetime.date
    status: PlatformFeeInvoice.InvoiceStatus

    fee_gross: Decimal
    fee_net: Decimal
    fee_vat: Decimal
    fee_vat_rate: Decimal
    currency: str
    reverse_charge: bool

    org_name: str
    org_vat_id: str
    org_vat_country: str

    total_tickets: int
    total_ticket_revenue: Decimal

    issued_at: AwareDatetime | None = None
    created_at: AwareDatetime


class InvoiceDownloadURLSchema(Schema):
    """Schema for invoice PDF download URL response."""

    download_url: str


class PlatformFeeCreditNoteSchema(Schema):
    """Schema for platform fee credit note responses."""

    id: UUID
    credit_note_number: str
    invoice_id: UUID

    fee_gross: Decimal
    fee_net: Decimal
    fee_vat: Decimal

    issued_at: AwareDatetime | None = None
    created_at: AwareDatetime


# ---- Attendee Invoice Schemas ----


class InvoicingModeUpdateSchema(Schema):
    """Schema for updating the organization's invoicing mode."""

    mode: Organization.InvoicingMode


class InvoiceLineItemSchema(Schema):
    """Schema for a single line item in an attendee invoice."""

    description: str
    unit_price_gross: Decimal
    discount_amount: Decimal = Decimal("0.00")
    net_amount: Decimal
    vat_amount: Decimal
    vat_rate: Decimal


class AttendeeInvoiceSchema(Schema):
    """Schema for attendee invoice list/detail responses."""

    id: UUID
    invoice_number: str
    status: AttendeeInvoice.InvoiceStatus
    total_gross: Decimal
    total_net: Decimal
    total_vat: Decimal
    vat_rate: Decimal
    currency: str
    reverse_charge: bool
    seller_name: str
    buyer_name: str
    buyer_email: str
    line_items: list[InvoiceLineItemSchema]
    discount_code_text: str
    discount_amount_total: Decimal
    issued_at: AwareDatetime | None = None
    created_at: AwareDatetime


class AttendeeInvoiceDetailSchema(AttendeeInvoiceSchema):
    """Extended schema with full seller/buyer info for org admin views."""

    seller_vat_id: str
    seller_vat_country: str
    seller_address: str
    seller_email: str
    buyer_vat_id: str
    buyer_vat_country: str
    buyer_address: str


class UpdateAttendeeInvoiceSchema(Schema):
    """Schema for editing a DRAFT attendee invoice.

    All fields except seller (org) info are editable.
    """

    buyer_name: str | None = None
    buyer_vat_id: str | None = None
    buyer_vat_country: str | None = None
    buyer_address: str | None = None
    buyer_email: str | None = None
    total_gross: Decimal | None = None
    total_net: Decimal | None = None
    total_vat: Decimal | None = None
    vat_rate: Decimal | None = None
    currency: str | None = None
    reverse_charge: bool | None = None
    discount_code_text: str | None = None
    discount_amount_total: Decimal | None = None
    line_items: list[InvoiceLineItemSchema] | None = None


class AttendeeInvoiceCreditNoteSchema(Schema):
    """Schema for attendee invoice credit note responses."""

    id: UUID
    credit_note_number: str
    invoice_number: str
    amount_gross: Decimal
    amount_net: Decimal
    amount_vat: Decimal
    issued_at: AwareDatetime | None = None
    created_at: AwareDatetime

    @staticmethod
    def resolve_invoice_number(obj: t.Any) -> str:
        """Resolve invoice number from the related invoice."""
        return str(obj.invoice.invoice_number)
