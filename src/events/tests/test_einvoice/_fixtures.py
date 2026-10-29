"""Builders for the e-invoice tests (#1235).

Documents are written straight to the ORM with the same arithmetic generation uses
(:func:`compute_en16931_totals`, :func:`credit_note_totals`), so every fixture carries the
per-group guarantees a real EN 16931 row has.
"""

import datetime
import typing as t
from decimal import Decimal
from itertools import count

from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import (
    AttendeeInvoice,
    AttendeeInvoiceCreditNote,
    InvoiceLineItemDict,
    TotalsMethod,
)
from events.service.credit_note_totals import credit_note_totals
from events.service.en16931_totals_service import compute_en16931_totals
from events.service.payment_means_service import PaymentMeans
from events.utils.vat_category import VatCategory, build_vat_exemptions

_SEQ = count(1)
CARD = PaymentMeans("48", "Paid by card", "4242")
OTHER = PaymentMeans("ZZZ", "Paid online via Stripe (PayPal)", "")


def line(gross: str, rate: str, category: str = "S", description: str = "Event — Tier — Guest") -> InvoiceLineItemDict:
    """A line with its charged gross, rate and category; net and VAT are filled in by the totals service."""
    return InvoiceLineItemDict(
        description=description,
        unit_price_gross=gross,
        discount_amount="0.00",
        net_amount="0.00",
        vat_amount="0.00",
        vat_rate=rate,
        vat_category=category,
    )


def make_invoice(
    organization: Organization,
    event: Event,
    user: RevelUser,
    *,
    lines: list[InvoiceLineItemDict],
    status: str = AttendeeInvoice.InvoiceStatus.ISSUED,
    totals_method: str = TotalsMethod.EN16931,
    reverse_charge: bool = False,
    seller_vat_id: str = "DE811234567",
    seller_country: str = "DE",
    buyer_vat_id: str = "DE123456789",
    buyer_country: str = "DE",
    payment: PaymentMeans = CARD,
    supply_date: datetime.date | None = datetime.date(2026, 3, 14),
    supply_period_end: datetime.date | None = None,
    exempt_reason: str = "",
    issued_at: datetime.datetime | None = None,
    **overrides: t.Any,
) -> AttendeeInvoice:
    """An EN 16931 invoice whose header totals are derived from ``lines`` the way generation does it."""
    n = next(_SEQ)
    totals = compute_en16931_totals(lines, reverse_charge)
    categories = [VatCategory(item["vat_category"]) for item in totals.lines]
    fields: dict[str, t.Any] = {
        "organization": organization,
        "event": event,
        "user": user,
        "stripe_session_id": f"cs_einv_{n}",
        "invoice_number": f"EINV-2026-{n:06d}",
        "status": status,
        "total_gross": totals.total_gross,
        "total_net": totals.total_net,
        "total_vat": totals.total_vat,
        "rounding_amount": totals.rounding_amount,
        "totals_method": totals_method,
        "vat_rate": Decimal(lines[0]["vat_rate"]),
        "currency": "EUR",
        "reverse_charge": reverse_charge,
        "line_items": totals.lines,
        "vat_exemptions": build_vat_exemptions(categories, [exempt_reason]),
        "supply_date": supply_date,
        "supply_period_end": supply_period_end,
        "payment_means_code": payment.code,
        "payment_method_label": payment.label,
        "card_last4": payment.card_last4,
        "seller_name": "ACME GmbH",
        "seller_vat_id": seller_vat_id,
        "seller_vat_country": seller_country,
        "seller_address_line1": "Musterstraße 1",
        "seller_postal_code": "10115",
        "seller_city": "Berlin",
        "seller_country_code": seller_country,
        "seller_email": "billing@acme.example",
        "buyer_name": "Buyer GmbH",
        "buyer_vat_id": buyer_vat_id,
        "buyer_vat_country": buyer_country,
        "buyer_address_line1": "Berliner Str. 1",
        "buyer_postal_code": "10115",
        "buyer_city": "Berlin",
        "buyer_subdivision": "BE",
        "buyer_country_code": buyer_country,
        "buyer_email": "buyer@example.de",
        "issued_at": issued_at or (timezone.now() if status != AttendeeInvoice.InvoiceStatus.DRAFT else None),
    }
    fields.update(overrides)
    return AttendeeInvoice.objects.create(**fields)


def make_credit_note(
    invoice: AttendeeInvoice,
    lines: list[InvoiceLineItemDict],
    *,
    totals_method: str | None = None,
    issued_at: datetime.datetime | None = None,
) -> AttendeeInvoiceCreditNote:
    """A credit note over ``lines``, figured by the invoice's method against its prior notes."""
    prior = list(invoice.credit_notes.all())
    totals = credit_note_totals(invoice, lines, prior)
    return AttendeeInvoiceCreditNote.objects.create(
        invoice=invoice,
        credit_note_number=f"EINV-CN-2026-{next(_SEQ):06d}",
        amount_gross=totals.amount_gross,
        amount_net=totals.amount_net,
        amount_vat=totals.amount_vat,
        rounding_amount=totals.rounding_amount,
        totals_method=totals_method or totals.totals_method,
        line_items=totals.lines,
        issued_at=issued_at or timezone.now(),
    )
