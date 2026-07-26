"""Platform fee invoice generation service.

Aggregates payment data, generates PDF invoices via WeasyPrint,
and handles invoice numbering and delivery.
"""

import typing as t
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, QuerySet, Sum
from django.utils import timezone

from common.models import SiteSettings
from common.service.invoice_utils import (
    format_currency as format_currency,
)
from common.service.invoice_utils import (
    get_next_sequential_number,
    render_pdf,
)
from events.models.invoice import PlatformFeeCreditNote, PlatformFeeInvoice
from events.models.organization import Organization
from events.models.subscription import MembershipPayment
from events.models.ticket import Payment

logger = structlog.get_logger(__name__)


class CombinedAggregate(t.TypedDict):
    """Ticket + membership platform fees merged for a single organization x currency."""

    currency: str
    fee_gross: Decimal
    fee_net: Decimal
    fee_vat: Decimal
    ticket_count: int
    ticket_revenue: Decimal
    subscription_count: int
    subscription_revenue: Decimal


def _get_next_invoice_number(year: int) -> str:
    """Generate the next sequential invoice number (e.g., RVL-2026-000001).

    Must be called inside ``transaction.atomic()``.
    """
    return get_next_sequential_number(PlatformFeeInvoice, "RVL-", year, "invoice_number")


def _get_next_credit_note_number(year: int) -> str:
    """Generate the next sequential credit note number (e.g., RVL-CN-2026-000001).

    Must be called inside ``transaction.atomic()``.
    """
    return get_next_sequential_number(PlatformFeeCreditNote, "RVL-CN-", year, "credit_note_number")


def render_invoice_pdf(invoice: PlatformFeeInvoice) -> bytes:
    """Render an invoice as a PDF using WeasyPrint."""
    return render_pdf(
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
            "total_subscription_payments": invoice.total_subscription_payments,
            "total_subscription_revenue": invoice.total_subscription_revenue,
        },
    )


def ensure_invoice_pdf_exists(invoice: PlatformFeeInvoice) -> None:
    """Regenerate the invoice PDF if it's missing.

    The PDF is saved outside the creation transaction (WeasyPrint is slow), so a
    worker crash can leave an ISSUED invoice without one. The recovery sweep calls
    this before re-sending so a lost document self-heals (issue #616).
    """
    if invoice.pdf_file:
        return
    pdf_bytes = render_invoice_pdf(invoice)
    invoice.pdf_file.save(f"{invoice.invoice_number}.pdf", ContentFile(pdf_bytes), save=True)


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


def _create_org_invoice(
    *,
    org: Organization,
    agg: CombinedAggregate,
    period_start: date,
    period_end: date,
    period_payments: QuerySet[Payment],
    period_membership_payments: QuerySet[MembershipPayment],
    site: SiteSettings,
    year: int,
    now: datetime,
) -> PlatformFeeInvoice | None:
    """Create a single platform fee invoice for an organization + currency.

    Returns the created invoice, or None if it already exists or was skipped.
    """
    org_id = org.id
    currency = agg["currency"]

    fee_gross = agg["fee_gross"]
    fee_net = agg["fee_net"]
    fee_vat = agg["fee_vat"]

    org_payments = period_payments.filter(
        ticket__event__organization_id=org_id,
        currency=currency,
    )
    # Only fee-bearing membership payments inform the VAT treatment: OFFLINE /
    # staff-recorded rows carry no platform fee, so their (default) non-RC flag
    # would otherwise wrongly break an all-reverse-charge invoice.
    org_membership_payments = period_membership_payments.filter(
        subscription__organization_id=org_id,
        currency=currency,
        platform_fee__gt=0,
    )
    fee_vat_rate, reverse_charge = _determine_vat_rate_and_reverse_charge(org_payments, org_membership_payments)

    try:
        with transaction.atomic():
            # Idempotency check inside transaction to prevent race conditions.
            if PlatformFeeInvoice.objects.filter(
                organization_id=org_id,
                period_start=period_start,
                currency=currency,
            ).exists():
                logger.info("invoice_already_exists", org_id=str(org_id), period=str(period_start), currency=currency)
                return None

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
                total_ticket_revenue=agg["ticket_revenue"],
                total_subscription_payments=agg["subscription_count"],
                total_subscription_revenue=agg["subscription_revenue"],
                # Status
                status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
                issued_at=now,
            )
    except IntegrityError:
        logger.info("invoice_duplicate_skipped", org_id=str(org_id), period=str(period_start), currency=currency)
        return None

    # Generate and attach PDF outside transaction (WeasyPrint is slow)
    pdf_bytes = render_invoice_pdf(invoice)
    invoice.pdf_file.save(
        f"{invoice_number}.pdf",
        ContentFile(pdf_bytes),
        save=True,
    )

    logger.info(
        "invoice_generated",
        invoice_number=invoice_number,
        org_id=str(org_id),
        fee_gross=str(fee_gross),
        currency=currency,
    )
    return invoice


def generate_invoices_for_period(
    period_start: date,
    period_end: date,
) -> list[PlatformFeeInvoice]:
    """Generate platform fee invoices for all organizations for a given period.

    Aggregates from Payment and MembershipPayment records (which snapshot the VAT
    rate at purchase time). Creates one invoice per organization x currency
    combination, covering both fee sources.
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

    # Use timezone-aware datetime boundaries so the created_at index is used
    # (created_at__date__gte forces a DATE() cast in SQL, bypassing the index)
    period_start_dt = timezone.make_aware(datetime.combine(period_start, time.min))
    period_end_dt = timezone.make_aware(datetime.combine(period_end + timedelta(days=1), time.min))

    period_payments = Payment.objects.filter(
        status=Payment.PaymentStatus.SUCCEEDED,
        created_at__gte=period_start_dt,
        created_at__lt=period_end_dt,
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

    period_membership_payments = MembershipPayment.objects.filter(
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        created_at__gte=period_start_dt,
        created_at__lt=period_end_dt,
    )

    # Same aggregation over subscription fees; merged below so an org billed on
    # both sources gets one invoice per currency instead of two. Zero-fee rows
    # (OFFLINE / staff-recorded) are dropped before the .values() grouping so the
    # payment count and revenue stats only describe what the fee was charged
    # against — matching the ticket side, which is online-only by construction.
    membership_aggregates = (
        period_membership_payments.filter(platform_fee__gt=0)
        .values(
            "subscription__organization_id",
            "currency",
        )
        .annotate(
            total_platform_fee=Sum("platform_fee"),
            total_platform_fee_net=Sum("platform_fee_net"),
            total_platform_fee_vat=Sum("platform_fee_vat"),
            total_amount=Sum("amount"),
            payment_count=Count("id"),
        )
        .filter(total_platform_fee__gt=0)
    )

    combined: dict[tuple[UUID, str], CombinedAggregate] = {}

    def _slot(org_id: UUID, currency: str) -> CombinedAggregate:
        return combined.setdefault(
            (org_id, currency),
            CombinedAggregate(
                currency=currency,
                fee_gross=Decimal("0.00"),
                fee_net=Decimal("0.00"),
                fee_vat=Decimal("0.00"),
                ticket_count=0,
                ticket_revenue=Decimal("0.00"),
                subscription_count=0,
                subscription_revenue=Decimal("0.00"),
            ),
        )

    for agg in aggregates:
        slot = _slot(agg["ticket__event__organization_id"], agg["currency"])
        gross = agg["total_platform_fee"] or Decimal("0.00")
        slot["fee_gross"] += gross
        # The net/VAT fallbacks are applied per source, so a pre-VAT source keeps
        # falling back to its own gross rather than to the combined total.
        slot["fee_net"] += agg["total_platform_fee_net"] or gross
        slot["fee_vat"] += agg["total_platform_fee_vat"] or Decimal("0.00")
        slot["ticket_count"] += agg["ticket_count"]
        slot["ticket_revenue"] += agg["total_amount"] or Decimal("0.00")

    for agg in membership_aggregates:
        slot = _slot(agg["subscription__organization_id"], agg["currency"])
        gross = agg["total_platform_fee"] or Decimal("0.00")
        slot["fee_gross"] += gross
        slot["fee_net"] += agg["total_platform_fee_net"] or gross
        slot["fee_vat"] += agg["total_platform_fee_vat"] or Decimal("0.00")
        slot["subscription_count"] += agg["payment_count"]
        slot["subscription_revenue"] += agg["total_amount"] or Decimal("0.00")

    # Prefetch all orgs that have payments to avoid N+1 queries in the loop
    org_ids = {org_id for org_id, _ in combined}
    orgs_by_id = {org.id: org for org in Organization.objects.select_related("owner").filter(id__in=org_ids)}

    created_invoices: list[PlatformFeeInvoice] = []

    for (org_id, _currency), combined_agg in combined.items():
        org = orgs_by_id.get(org_id)
        if not org:
            logger.warning("org_not_found_for_invoice", org_id=str(org_id))
            continue

        invoice = _create_org_invoice(
            org=org,
            agg=combined_agg,
            period_start=period_start,
            period_end=period_end,
            period_payments=period_payments,
            period_membership_payments=period_membership_payments,
            site=site,
            year=year,
            now=now,
        )
        if invoice:
            created_invoices.append(invoice)

    return created_invoices


def _determine_vat_rate_and_reverse_charge(
    payments: QuerySet[Payment],
    membership_payments: QuerySet[MembershipPayment] | None = None,
) -> tuple[Decimal, bool]:
    """Determine VAT rate and reverse charge from actual payment records.

    For reverse charge, reads the persisted boolean from Payment. Ticket and
    membership payments snapshot the fee VAT under identical field names, so both
    sources are pooled into a single decision for the invoice.
    Only marks as reverse charge if ALL payments in the period used it
    (a mix means the org's status changed mid-period — use normal VAT).

    The VAT rate is the dominant rate (most common across payments).
    This is informational on the invoice; actual fee_vat is the precise sum
    of individually-calculated payment amounts, so mid-month rate changes
    are handled correctly in the totals even if the displayed rate is approximate.

    Returns:
        Tuple of (fee_vat_rate, reverse_charge).
    """
    sources: list[QuerySet[Payment] | QuerySet[MembershipPayment]] = [payments]
    if membership_payments is not None:
        sources.append(membership_payments)

    total = sum(source.count() for source in sources)
    rc_count = sum(source.filter(platform_fee_reverse_charge=True).count() for source in sources)

    # Only mark as reverse charge if ALL payments used it
    if total > 0 and rc_count == total:
        return Decimal("0.00"), True

    # For VAT rate, find the dominant rate across non-RC payments of both sources
    rate_counts: Counter[Decimal] = Counter()
    for source in sources:
        for row in (
            source.filter(platform_fee_vat_rate__isnull=False, platform_fee_reverse_charge=False)
            .values("platform_fee_vat_rate")
            .annotate(cnt=Count("id"))
        ):
            rate_counts[row["platform_fee_vat_rate"]] += row["cnt"]

    if not rate_counts:
        # Fallback for pre-VAT payments
        return Decimal("0.00"), False

    return rate_counts.most_common(1)[0][0], False


def generate_monthly_invoices() -> list[PlatformFeeInvoice]:
    """Generate invoices for the previous month.

    Intended to be called on the 1st of each month.
    """
    today = timezone.now().date()
    # Previous month
    first_of_current = today.replace(day=1)
    last_of_previous = first_of_current - timedelta(days=1)
    first_of_previous = last_of_previous.replace(day=1)

    return generate_invoices_for_period(first_of_previous, last_of_previous)
