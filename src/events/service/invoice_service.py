"""Platform fee invoice generation service.

Aggregates payment data, generates PDF invoices via WeasyPrint,
and handles invoice numbering and delivery.
"""

import typing as t
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import structlog
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, QuerySet, Sum
from django.template.loader import render_to_string
from django.utils import timezone
from weasyprint import HTML

from common.models import SiteSettings
from events.models.invoice import PlatformFeeCreditNote, PlatformFeeInvoice
from events.models.organization import Organization
from events.models.ticket import Payment

logger = structlog.get_logger(__name__)

CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "\u20ac",
    "USD": "$",
    "GBP": "\u00a3",
    "CHF": "CHF ",
    "DKK": "DKK ",
    "SEK": "SEK ",
    "NOK": "NOK ",
    "PLN": "PLN ",
    "CZK": "CZK ",
    "HUF": "HUF ",
    "RON": "RON ",
    "BGN": "BGN ",
}


def format_currency(value: Decimal | float, currency: str = "EUR") -> str:
    """Format a value with a currency symbol."""
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    return f"{symbol}{value:,.2f}"


def _get_next_sequential_number(model: t.Any, prefix: str, year: int, number_field: str) -> str:
    """Generate the next sequential document number for a given year.

    Format: {prefix}{YEAR}-{SEQUENCE:06d}

    Must be called inside ``transaction.atomic()`` — uses SELECT FOR UPDATE
    on the last record to serialize concurrent access.

    Args:
        model: The Django model class (PlatformFeeInvoice or PlatformFeeCreditNote).
        prefix: Number prefix including trailing dash (e.g., "RVL-" or "RVL-CN-").
        year: The fiscal year.
        number_field: Name of the CharField holding the number (e.g., "invoice_number").

    Returns:
        The next sequential number string.
    """
    full_prefix = f"{prefix}{year}-"
    last = (
        model.objects.select_for_update()
        .filter(**{f"{number_field}__startswith": full_prefix})
        .order_by(f"-{number_field}")
        .first()
    )
    if last:
        last_seq = int(getattr(last, number_field).split("-")[-1])
        return f"{full_prefix}{last_seq + 1:06d}"
    return f"{full_prefix}{1:06d}"


def _get_next_invoice_number(year: int) -> str:
    """Generate the next sequential invoice number (e.g., RVL-2026-000001).

    Must be called inside ``transaction.atomic()``.
    """
    return _get_next_sequential_number(PlatformFeeInvoice, "RVL-", year, "invoice_number")


def _get_next_credit_note_number(year: int) -> str:
    """Generate the next sequential credit note number (e.g., RVL-CN-2026-000001).

    Must be called inside ``transaction.atomic()``.
    """
    return _get_next_sequential_number(PlatformFeeCreditNote, "RVL-CN-", year, "credit_note_number")


def _render_invoice_pdf(invoice: PlatformFeeInvoice) -> bytes:
    """Render an invoice as a PDF using WeasyPrint."""
    html_content = render_to_string(
        "invoices/platform_fee_invoice.html",
        {
            "platform_business_name": invoice.platform_business_name,
            "platform_business_address": invoice.platform_business_address,
            "platform_vat_id": invoice.platform_vat_id,
            "invoice_number": invoice.invoice_number,
            "issued_date": invoice.issued_at.strftime("%Y-%m-%d") if invoice.issued_at else "",
            "period_start": invoice.period_start.isoformat(),
            "period_end": invoice.period_end.isoformat(),
            "period_label": invoice.period_start.strftime("%B %Y"),
            "currency": invoice.currency,
            "org_name": invoice.org_name,
            "org_address": invoice.org_address,
            "org_vat_id": invoice.org_vat_id,
            "fee_gross": invoice.fee_gross,
            "fee_net": invoice.fee_net,
            "fee_vat": invoice.fee_vat,
            "fee_vat_rate": invoice.fee_vat_rate,
            "reverse_charge": invoice.reverse_charge,
            "total_tickets": invoice.total_tickets,
            "total_ticket_revenue": invoice.total_ticket_revenue,
        },
    )
    pdf_buffer = BytesIO()
    HTML(string=html_content).write_pdf(pdf_buffer)
    return pdf_buffer.getvalue()


def get_invoice_recipients(org: Organization) -> list[str]:
    """Get the list of email recipients for an invoice.

    Returns:
        List of email addresses: org owner + billing_email (or contact_email fallback).
    """
    recipients: list[str] = []

    # Owner email
    if org.owner.email:
        recipients.append(org.owner.email)

    # Billing email, falling back to contact email
    billing_email = org.billing_email or org.contact_email
    if billing_email and billing_email not in recipients:
        recipients.append(billing_email)

    return recipients


def generate_invoices_for_period(
    period_start: date,
    period_end: date,
) -> list[PlatformFeeInvoice]:
    """Generate platform fee invoices for all organizations for a given period.

    Aggregates from Payment records (which snapshot the VAT rate at purchase time).
    Creates one invoice per organization x currency combination.
    Skips organizations with zero successful payments in the period.

    Counts individual Payment records (one per ticket) for the total_tickets stat.

    Args:
        period_start: First day of the period (inclusive).
        period_end: Last day of the period (inclusive).

    Returns:
        List of created PlatformFeeInvoice records.
    """
    site = SiteSettings.get_solo()
    now = timezone.now()
    year = period_start.year

    period_payments = Payment.objects.filter(
        status=Payment.PaymentStatus.SUCCEEDED,
        created_at__date__gte=period_start,
        created_at__date__lte=period_end,
    )

    # Aggregate payments by org + currency
    aggregates = (
        period_payments.values(
            "ticket__event__organization_id",
            "currency",
        )
        .annotate(
            total_platform_fee=Sum("platform_fee"),
            total_platform_fee_net=Sum("platform_fee_net"),
            total_platform_fee_vat=Sum("platform_fee_vat"),
            total_amount=Sum("amount"),
            ticket_count=Count("id"),
        )
        .filter(total_platform_fee__gt=0)
    )

    # Prefetch all orgs that have payments to avoid N+1 queries in the loop
    org_ids = {agg["ticket__event__organization_id"] for agg in aggregates}
    orgs_by_id = {org.id: org for org in Organization.objects.select_related("owner").filter(id__in=org_ids)}

    created_invoices: list[PlatformFeeInvoice] = []

    for agg in aggregates:
        org_id = agg["ticket__event__organization_id"]
        currency = agg["currency"]

        org = orgs_by_id.get(org_id)
        if not org:
            logger.warning("org_not_found_for_invoice", org_id=str(org_id))
            continue

        fee_gross = agg["total_platform_fee"] or Decimal("0.00")
        fee_net = agg["total_platform_fee_net"] or fee_gross  # fallback for pre-VAT payments
        fee_vat = agg["total_platform_fee_vat"] or Decimal("0.00")

        org_payments = period_payments.filter(
            ticket__event__organization_id=org_id,
            currency=currency,
        )
        fee_vat_rate, reverse_charge = _determine_vat_rate_and_reverse_charge(org_payments)

        try:
            with transaction.atomic():
                # Idempotency check inside transaction to prevent race conditions.
                # The UniqueConstraint is the ultimate guard, but checking first
                # avoids burning an invoice number on a duplicate.
                if PlatformFeeInvoice.objects.filter(
                    organization_id=org_id,
                    period_start=period_start,
                    currency=currency,
                ).exists():
                    logger.info(
                        "invoice_already_exists", org_id=str(org_id), period=str(period_start), currency=currency
                    )
                    continue

                invoice_number = _get_next_invoice_number(year)

                invoice = PlatformFeeInvoice.objects.create(
                    organization=org,
                    invoice_number=invoice_number,
                    period_start=period_start,
                    period_end=period_end,
                    fee_gross=fee_gross,
                    fee_net=fee_net,
                    fee_vat=fee_vat,
                    fee_vat_rate=fee_vat_rate,
                    currency=currency,
                    reverse_charge=reverse_charge,
                    # Organization snapshot
                    org_name=org.billing_name or org.name,
                    org_vat_id=org.vat_id,
                    org_vat_country=org.vat_country_code,
                    org_address=org.billing_address,
                    # Platform snapshot
                    platform_business_name=site.platform_business_name,
                    platform_business_address=site.platform_business_address,
                    platform_vat_id=site.platform_vat_id,
                    # Aggregate stats
                    total_tickets=agg["ticket_count"],
                    total_ticket_revenue=agg["total_amount"] or Decimal("0.00"),
                    # Status
                    status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
                    issued_at=now,
                )
        except IntegrityError:
            # Concurrent run already created this invoice — safe to skip
            logger.info("invoice_duplicate_skipped", org_id=str(org_id), period=str(period_start), currency=currency)
            continue

        # Generate and attach PDF outside transaction (WeasyPrint is slow)
        pdf_bytes = _render_invoice_pdf(invoice)
        invoice.pdf_file.save(
            f"{invoice_number}.pdf",
            ContentFile(pdf_bytes),
            save=True,
        )

        created_invoices.append(invoice)
        logger.info(
            "invoice_generated",
            invoice_number=invoice_number,
            org_id=str(org_id),
            fee_gross=str(fee_gross),
            currency=currency,
        )

    return created_invoices


def _determine_vat_rate_and_reverse_charge(
    payments: QuerySet[Payment],
) -> tuple[Decimal, bool]:
    """Determine VAT rate and reverse charge from actual payment records.

    For reverse charge, reads the persisted boolean from Payment.
    Only marks as reverse charge if ALL payments in the period used it
    (a mix means the org's status changed mid-period — use normal VAT).

    The VAT rate is the dominant rate (most common across payments).
    This is informational on the invoice; actual fee_vat is the precise sum
    of individually-calculated payment amounts, so mid-month rate changes
    are handled correctly in the totals even if the displayed rate is approximate.

    Returns:
        Tuple of (fee_vat_rate, reverse_charge).
    """
    total = payments.count()
    rc_count = payments.filter(platform_fee_reverse_charge=True).count()

    # Only mark as reverse charge if ALL payments used it
    if total > 0 and rc_count == total:
        return Decimal("0.00"), True

    # For VAT rate, find the dominant rate across non-RC payments
    rate_counts = (
        payments.filter(platform_fee_vat_rate__isnull=False, platform_fee_reverse_charge=False)
        .values("platform_fee_vat_rate")
        .annotate(cnt=Count("id"))
        .order_by("-cnt")
    )
    if rate_counts:
        fee_vat_rate = rate_counts[0]["platform_fee_vat_rate"]
    else:
        # Fallback for pre-VAT payments
        fee_vat_rate = Decimal("0.00")

    return fee_vat_rate, False


def generate_monthly_invoices() -> list[PlatformFeeInvoice]:
    """Generate invoices for the previous month.

    Intended to be called on the 1st of each month.
    """
    today = date.today()
    # Previous month
    first_of_current = today.replace(day=1)
    last_of_previous = first_of_current - timedelta(days=1)
    first_of_previous = last_of_previous.replace(day=1)

    return generate_invoices_for_period(first_of_previous, last_of_previous)
