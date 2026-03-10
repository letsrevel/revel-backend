"""Platform fee invoice and credit note schemas."""

import datetime
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime

from events.models.invoice import PlatformFeeInvoice


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
