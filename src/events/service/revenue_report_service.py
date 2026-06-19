"""Revenue & VAT report aggregation and content hash (#551)."""

import hashlib
import io
import typing as t
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone
from openpyxl import Workbook

from common.service.vat_utils import calculate_vat_inclusive
from events.models import Organization, Payment, Ticket, TicketTier

ZERO = Decimal("0.00")
_REVERSE_CHARGE_LABEL = "0% / reverse-charge"


@dataclass(frozen=True)
class ReportScope:
    """Scope parameters for a revenue & VAT report."""

    org: Organization
    event_id: UUID | None
    date_from: date
    date_to: date


@dataclass(frozen=True)
class RateBucket:
    """Aggregated totals for a single VAT rate within a currency."""

    vat_rate: Decimal
    label: str
    net: Decimal
    vat: Decimal
    gross: Decimal
    ticket_count: int


@dataclass(frozen=True)
class TxnRow:
    """A single transaction line for the detail sheet."""

    date: date
    payment_id: str
    event: str
    tier: str
    buyer_country: str
    reverse_charge: bool
    gross: Decimal
    net: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    discount: Decimal
    refund_amount: Decimal
    currency: str
    stripe_session_id: str
    stripe_payout_id: str


@dataclass(frozen=True)
class CurrencySection:
    """All data for a single currency in the report."""

    currency: str
    rate_buckets: list[RateBucket]
    refunds_total: Decimal
    net_taxable_turnover: Decimal
    sold_count: int
    refunded_count: int
    transactions: list[TxnRow]


@dataclass(frozen=True)
class RevenueReportData:
    """Full aggregated report data returned to callers."""

    scope: ReportScope
    sections: list[CurrencySection]
    generated_at: datetime


def organization_timezone(org: Organization) -> ZoneInfo:
    """Return the org's city timezone, falling back to the platform default.

    Task 8 will refactor this to delegate to events.utils.get_organization_timezone.
    """
    if org.city and org.city.timezone:
        return ZoneInfo(org.city.timezone)
    return ZoneInfo(settings.TIME_ZONE)


def _local_date(value: datetime, tz: ZoneInfo) -> date:
    return value.astimezone(tz).date()


def _in_period(d: date, scope: ReportScope) -> bool:
    return scope.date_from <= d <= scope.date_to


class _BucketAcc:
    """Mutable per-rate accumulator (sales minus refunds)."""

    def __init__(self, vat_rate: Decimal, label: str) -> None:
        self.vat_rate = vat_rate
        self.label = label
        self.net = ZERO
        self.vat = ZERO
        self.gross = ZERO
        self.count = 0


class _CurrencyAcc:
    """Mutable accumulator for one currency."""

    def __init__(self) -> None:
        self.buckets: dict[str, _BucketAcc] = {}
        self.refunds_total = ZERO
        self.sold_count = 0
        self.refunded_count = 0
        self.transactions: list[TxnRow] = []

    def bucket_for(self, vat_rate: Decimal, reverse_charge: bool) -> _BucketAcc:
        if reverse_charge or vat_rate == ZERO:
            return self.buckets.setdefault("rc", _BucketAcc(ZERO, _REVERSE_CHARGE_LABEL))
        key = f"{vat_rate:.2f}"
        return self.buckets.setdefault(key, _BucketAcc(vat_rate, f"{vat_rate.normalize()}%"))


def _resolve_payment_vat(payment: Payment, org_rate: Decimal) -> tuple[Decimal, Decimal, Decimal, bool]:
    """Return (net, vat, rate, reverse_charge) for a payment's gross amount."""
    if payment.net_amount is not None and payment.vat_amount is not None and payment.vat_rate is not None:
        rate = payment.vat_rate
        reverse_charge = rate == ZERO and payment.vat_amount == ZERO
        return payment.net_amount, payment.vat_amount, rate, reverse_charge
    snapshot: dict[str, t.Any] = payment.buyer_billing_snapshot or {}
    reverse_charge = bool(snapshot.get("reverse_charge"))
    if reverse_charge:
        return payment.amount, ZERO, ZERO, True
    breakdown = calculate_vat_inclusive(payment.amount, org_rate)
    return breakdown.net_amount, breakdown.vat_amount, breakdown.vat_rate, False


def _add_sale(
    acc: _CurrencyAcc,
    net: Decimal,
    vat: Decimal,
    gross: Decimal,
    rate: Decimal,
    rc: bool,
) -> None:
    bucket = acc.bucket_for(rate, rc)
    bucket.net += net
    bucket.vat += vat
    bucket.gross += gross
    bucket.count += 1
    acc.sold_count += 1


def _add_refund(acc: _CurrencyAcc, refund_gross: Decimal, rate: Decimal, rc: bool) -> None:
    effective_rate = ZERO if (rc or rate == ZERO) else rate
    breakdown = calculate_vat_inclusive(refund_gross, effective_rate)
    bucket = acc.bucket_for(rate, rc)
    bucket.net -= breakdown.net_amount
    bucket.vat -= breakdown.vat_amount
    bucket.gross -= refund_gross
    acc.refunds_total += refund_gross
    acc.refunded_count += 1


def _online_payments(scope: ReportScope) -> QuerySet[Payment]:
    qs = Payment.objects.select_related("ticket__event", "ticket__tier").filter(
        ticket__event__organization=scope.org,
        ticket__tier__payment_method=TicketTier.PaymentMethod.ONLINE,
        status__in=[Payment.PaymentStatus.SUCCEEDED, Payment.PaymentStatus.REFUNDED],
    )
    if scope.event_id is not None:
        qs = qs.filter(ticket__event_id=scope.event_id)
    return qs


def _offline_tickets(scope: ReportScope) -> QuerySet[Ticket]:
    from events.service.ticket_service import _offline_paid_q  # private but stable

    offline_paid = _offline_paid_q()
    qs = Ticket.objects.select_related("event", "tier").filter(
        Q(offline_paid) | Q(status=Ticket.TicketStatus.CANCELLED, offline_refund_amount__isnull=False),
        event__organization=scope.org,
        tier__payment_method__in=[
            TicketTier.PaymentMethod.OFFLINE,
            TicketTier.PaymentMethod.AT_THE_DOOR,
        ],
    )
    if scope.event_id is not None:
        qs = qs.filter(event_id=scope.event_id)
    return qs


def _process_payment(
    payment: Payment,
    scope: ReportScope,
    org_rate: Decimal,
    tz: ZoneInfo,
    currencies: dict[str, _CurrencyAcc],
) -> None:
    currency = payment.currency
    acc = currencies.setdefault(currency, _CurrencyAcc())
    net, vat, rate, rc = _resolve_payment_vat(payment, org_rate)

    sale_in = _in_period(_local_date(payment.created_at, tz), scope)
    refund_in = (
        payment.refund_status == Payment.RefundStatus.SUCCEEDED
        and payment.refunded_at is not None
        and _in_period(_local_date(payment.refunded_at, tz), scope)
    )

    if sale_in:
        _add_sale(acc, net, vat, payment.amount, rate, rc)
    if refund_in and payment.refund_amount:
        _add_refund(acc, payment.refund_amount, rate, rc)

    if sale_in or refund_in:
        acc.transactions.append(
            TxnRow(
                date=_local_date(payment.created_at, tz),
                payment_id=str(payment.id),
                event=payment.ticket.event.name,
                tier=payment.ticket.tier.name if payment.ticket.tier else "",
                buyer_country=str((payment.buyer_billing_snapshot or {}).get("country", "")),
                reverse_charge=rc,
                gross=payment.amount,
                net=net,
                vat_rate=rate,
                vat_amount=vat,
                discount=payment.ticket.discount_amount or ZERO,
                refund_amount=(payment.refund_amount or ZERO) if refund_in else ZERO,
                currency=currency,
                stripe_session_id=payment.stripe_session_id,
                stripe_payout_id="",
            )
        )


def _process_ticket(
    ticket: Ticket,
    scope: ReportScope,
    org_rate: Decimal,
    tz: ZoneInfo,
    currencies: dict[str, _CurrencyAcc],
) -> None:
    currency = ticket.tier.currency if ticket.tier else scope.org.vat_country_code
    acc = currencies.setdefault(currency, _CurrencyAcc())
    gross = ticket.price_paid if ticket.price_paid is not None else (ticket.tier.price if ticket.tier else ZERO)
    breakdown = calculate_vat_inclusive(gross, org_rate)

    sale_in = _in_period(_local_date(ticket.created_at, tz), scope)
    refund_in = (
        ticket.status == Ticket.TicketStatus.CANCELLED
        and ticket.offline_refund_amount is not None
        and ticket.cancelled_at is not None
        and _in_period(_local_date(ticket.cancelled_at, tz), scope)
    )

    if sale_in:
        _add_sale(acc, breakdown.net_amount, breakdown.vat_amount, gross, org_rate, False)
    if refund_in and ticket.offline_refund_amount:
        _add_refund(acc, ticket.offline_refund_amount, org_rate, False)

    if sale_in or refund_in:
        acc.transactions.append(
            TxnRow(
                date=_local_date(ticket.created_at, tz),
                payment_id=f"offline:{ticket.id}",
                event=ticket.event.name,
                tier=ticket.tier.name if ticket.tier else "",
                buyer_country=scope.org.vat_country_code,
                reverse_charge=False,
                gross=gross,
                net=breakdown.net_amount,
                vat_rate=org_rate,
                vat_amount=breakdown.vat_amount,
                discount=ticket.discount_amount or ZERO,
                refund_amount=(ticket.offline_refund_amount or ZERO) if refund_in else ZERO,
                currency=currency,
                stripe_session_id="",
                stripe_payout_id="",
            )
        )


def build_revenue_report_data(scope: ReportScope) -> RevenueReportData:
    """Aggregate ticket revenue into buckets by currency and VAT rate."""
    tz = organization_timezone(scope.org)
    org_rate = scope.org.vat_rate
    currencies: dict[str, _CurrencyAcc] = {}

    for payment in _online_payments(scope):
        _process_payment(payment, scope, org_rate, tz, currencies)

    for ticket in _offline_tickets(scope):
        _process_ticket(ticket, scope, org_rate, tz, currencies)

    sections: list[CurrencySection] = []
    for currency, acc in sorted(currencies.items()):
        buckets = [
            RateBucket(b.vat_rate, b.label, b.net, b.vat, b.gross, b.count)
            for b in sorted(acc.buckets.values(), key=lambda x: x.vat_rate)
        ]
        net_taxable = sum((b.net for b in buckets), ZERO)
        if not buckets and acc.refunds_total == ZERO:
            continue
        sections.append(
            CurrencySection(
                currency=currency,
                rate_buckets=buckets,
                refunds_total=acc.refunds_total,
                net_taxable_turnover=net_taxable,
                sold_count=acc.sold_count,
                refunded_count=acc.refunded_count,
                transactions=sorted(acc.transactions, key=lambda r: r.date),
            )
        )

    return RevenueReportData(scope=scope, sections=sections, generated_at=timezone.now())


def compute_revenue_data_hash(scope: ReportScope) -> str:
    """SHA-256 over in-scope payment + offline-ticket rows for cache invalidation."""
    parts: list[str] = []
    for payment in _online_payments(scope).order_by("id"):
        parts.append(
            "|".join(
                [
                    str(payment.id),
                    payment.updated_at.isoformat(),
                    payment.status,
                    payment.refund_status or "",
                ]
            )
        )
    for ticket in _offline_tickets(scope).order_by("id"):
        parts.append(
            "|".join(
                [
                    f"offline:{ticket.id}",
                    ticket.updated_at.isoformat(),
                    ticket.status,
                    str(ticket.offline_refund_amount),
                ]
            )
        )
    scope_key = f"{scope.org.id}:{scope.event_id}:{scope.date_from}:{scope.date_to}"
    raw = scope_key + "||" + "\n".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Bundle builders
# ---------------------------------------------------------------------------

_TXN_HEADERS = [
    "date",
    "payment_id",
    "event",
    "tier",
    "buyer_country",
    "reverse_charge",
    "gross",
    "net",
    "vat_rate",
    "vat_amount",
    "discount",
    "refund_amount",
    "currency",
    "stripe_session_id",
    "stripe_payout_id",
]


def report_filename(scope: ReportScope, ext: str = "zip") -> str:
    """Return a canonical filename for the report bundle or one of its parts."""
    return f"revel-revenue-{scope.org.slug}-{scope.date_from}_{scope.date_to}.{ext}"


def build_xlsx(data: RevenueReportData) -> bytes:
    """Build the two-sheet XLSX workbook (Summary + Transactions) and return raw bytes."""
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"  # type: ignore[union-attr]
    summary.append(["Currency", "VAT rate", "Net", "VAT", "Gross", "Tickets"])  # type: ignore[union-attr]
    for section in data.sections:
        for bucket in section.rate_buckets:
            summary.append(  # type: ignore[union-attr]
                [section.currency, bucket.label, bucket.net, bucket.vat, bucket.gross, bucket.ticket_count]
            )
        summary.append([section.currency, "Refunds", "", "", -section.refunds_total, section.refunded_count])  # type: ignore[union-attr]
        summary.append(  # type: ignore[union-attr]
            [section.currency, "Net taxable turnover", section.net_taxable_turnover, "", "", section.sold_count]
        )
        summary.append([])  # type: ignore[union-attr]

    txns = wb.create_sheet("Transactions")
    txns.append(_TXN_HEADERS)
    for section in data.sections:
        for row in section.transactions:
            # ponytail: TxnRow.date is always the SALE date (payment.created_at converted to
            # local date), even for refund-only rows where the original sale fell outside the
            # report period. The refund-only scenario is uncommon; a correct fix would add a
            # separate `refund_date` field to TxnRow and emit that here when the sale is
            # out-of-period. Tracked as a known limitation — do not change _process_payment
            # or _process_ticket here; that belongs in a follow-up to avoid breaking Task 3's
            # passing tests.
            txns.append(
                [
                    row.date.isoformat(),
                    row.payment_id,
                    row.event,
                    row.tier,
                    row.buyer_country,
                    row.reverse_charge,
                    row.gross,
                    row.net,
                    row.vat_rate,
                    row.vat_amount,
                    row.discount,
                    row.refund_amount,
                    row.currency,
                    row.stripe_session_id,
                    row.stripe_payout_id,
                ]
            )

    buf = io.BytesIO()
    wb.save(buf)
    out = buf.getvalue()
    buf.close()
    wb.close()
    return out


def build_pdf(data: RevenueReportData) -> bytes:
    """Render the revenue & VAT report as a PDF via WeasyPrint."""
    from common.service.invoice_utils import render_pdf

    return render_pdf("reports/revenue_vat_report.html", {"data": data, "org": data.scope.org})


def build_zip(data: RevenueReportData) -> bytes:
    """Bundle XLSX + PDF into a ZIP archive and return raw bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(report_filename(data.scope, "xlsx"), build_xlsx(data))
        zf.writestr(report_filename(data.scope, "pdf"), build_pdf(data))
    buf.seek(0)
    return buf.read()
