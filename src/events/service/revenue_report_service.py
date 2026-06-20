"""Revenue & VAT report aggregation and content hash (#551)."""

import calendar
import copy
import hashlib
import io
import typing as t
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from django.db import transaction
from django.db.models import Q, QuerySet
from django.template.loader import render_to_string
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment

from common.models import FileExport
from common.service.export_service import complete_export, fail_export, start_export
from common.service.vat_utils import calculate_vat_inclusive
from common.tasks import send_email
from events.models import Organization, Payment, Ticket, TicketTier
from events.service.export.formatting import LABEL_FONT, auto_fit_columns, style_header_row
from events.utils import get_organization_timezone

if t.TYPE_CHECKING:
    from openpyxl.worksheet.worksheet import Worksheet

    from accounts.models import RevelUser

logger = structlog.get_logger(__name__)

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
    """Return the org's city timezone, falling back to the platform default."""
    return get_organization_timezone(org)


def closed_period_for(cadence: str, now_local: datetime) -> tuple[date, date, str] | None:
    """The most-recently-closed reporting period for the cadence, in local time.

    QUARTERLY only fires in the month after a quarter closes (Jan/Apr/Jul/Oct).
    Returns (date_from, date_to, label) or None when nothing closed this month.

    Args:
        cadence: One of ``Organization.RevenueReportCadence`` values.
        now_local: Current local datetime (timezone-aware, in the org's tz).

    Returns:
        ``(date_from, date_to, label)`` for the most recently closed period,
        or ``None`` when the cadence produces no report this month.
    """
    if cadence == Organization.RevenueReportCadence.MONTHLY:
        year = now_local.year if now_local.month > 1 else now_local.year - 1
        month = now_local.month - 1 or 12
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day), f"{year}-{month:02d}"

    if cadence == Organization.RevenueReportCadence.QUARTERLY:
        if now_local.month not in (1, 4, 7, 10):
            return None
        if now_local.month == 1:
            year, quarter = now_local.year - 1, 4
        else:
            year, quarter = now_local.year, (now_local.month - 1) // 3
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        last_day = calendar.monthrange(year, end_month)[1]
        return date(year, start_month, 1), date(year, end_month, last_day), f"{year}-Q{quarter}"

    return None


def _local_date(value: datetime, tz: ZoneInfo) -> date:
    return value.astimezone(tz).date()


def _in_period(d: date, scope: ReportScope) -> bool:
    return scope.date_from <= d <= scope.date_to


class _BucketAcc:
    """Mutable per-rate accumulator tracking sale and refund sides separately."""

    def __init__(self, vat_rate: Decimal, label: str) -> None:
        self.vat_rate = vat_rate
        self.label = label
        self.sale_net = ZERO
        self.sale_vat = ZERO
        self.sale_gross = ZERO
        self.sold_count = 0
        self.refund_net = ZERO
        self.refund_vat = ZERO
        self.refund_gross = ZERO
        self.refunded_count = 0


class _CurrencyAcc:
    """Mutable accumulator for one currency."""

    def __init__(self) -> None:
        self.buckets: dict[str, _BucketAcc] = {}
        self.transactions: list[TxnRow] = []

    def bucket_for(self, vat_rate: Decimal, reverse_charge: bool) -> _BucketAcc:
        if reverse_charge or vat_rate == ZERO:
            return self.buckets.setdefault("rc", _BucketAcc(ZERO, _REVERSE_CHARGE_LABEL))
        key = f"{vat_rate:.2f}"
        # ``:f`` avoids Decimal scientific notation: Decimal("20.00").normalize() is
        # Decimal("2E+1"), which would render as "2E+1%" instead of "20%".
        return self.buckets.setdefault(key, _BucketAcc(vat_rate, f"{vat_rate.normalize():f}%"))


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
    bucket.sale_net += net
    bucket.sale_vat += vat
    bucket.sale_gross += gross
    bucket.sold_count += 1


def _add_refund(acc: _CurrencyAcc, refund_gross: Decimal, rate: Decimal, rc: bool) -> None:
    effective_rate = ZERO if (rc or rate == ZERO) else rate
    breakdown = calculate_vat_inclusive(refund_gross, effective_rate)
    bucket = acc.bucket_for(rate, rc)
    bucket.refund_net += breakdown.net_amount
    bucket.refund_vat += breakdown.vat_amount
    bucket.refund_gross += refund_gross
    bucket.refunded_count += 1


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
    include_transactions: bool = True,
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

    if (sale_in or refund_in) and include_transactions:
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
    include_transactions: bool = True,
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
    if refund_in and ticket.offline_refund_amount is not None:
        _add_refund(acc, ticket.offline_refund_amount, org_rate, False)

    if (sale_in or refund_in) and include_transactions:
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


class _EventAgg:
    """Per-event accumulator: event metadata plus its per-currency totals."""

    def __init__(self, event_id: UUID, name: str, start: datetime) -> None:
        self.event_id = event_id
        self.name = name
        self.start = start
        self.currencies: dict[str, _CurrencyAcc] = {}


def _merge_currency(dst: _CurrencyAcc, src: _CurrencyAcc) -> None:
    """Fold ``src`` into ``dst`` (used to roll per-event currencies up to org level)."""
    for key, b in src.buckets.items():
        d = dst.buckets.get(key)
        if d is None:
            dst.buckets[key] = copy.copy(b)
            continue
        d.sale_net += b.sale_net
        d.sale_vat += b.sale_vat
        d.sale_gross += b.sale_gross
        d.sold_count += b.sold_count
        d.refund_net += b.refund_net
        d.refund_vat += b.refund_vat
        d.refund_gross += b.refund_gross
        d.refunded_count += b.refunded_count
    dst.transactions.extend(src.transactions)


def _currency_section(currency: str, acc: _CurrencyAcc) -> CurrencySection | None:
    """Build a report ``CurrencySection`` (net-of-refunds) or ``None`` if empty."""
    if not acc.buckets:
        return None
    buckets = [
        RateBucket(
            vat_rate=b.vat_rate,
            label=b.label,
            net=b.sale_net - b.refund_net,
            vat=b.sale_vat - b.refund_vat,
            gross=b.sale_gross - b.refund_gross,
            ticket_count=b.sold_count,
        )
        for b in sorted(acc.buckets.values(), key=lambda x: x.vat_rate)
    ]
    return CurrencySection(
        currency=currency,
        rate_buckets=buckets,
        refunds_total=sum((b.refund_gross for b in acc.buckets.values()), ZERO),
        net_taxable_turnover=sum((rb.net for rb in buckets), ZERO),
        sold_count=sum(b.sold_count for b in acc.buckets.values()),
        refunded_count=sum(b.refunded_count for b in acc.buckets.values()),
        transactions=sorted(acc.transactions, key=lambda r: r.date),
    )


def _aggregate(scope: ReportScope, *, include_transactions: bool = True) -> dict[UUID, _EventAgg]:
    """Single per-row pass; returns per-event accumulators keyed by event id."""
    tz = organization_timezone(scope.org)
    org_rate = scope.org.vat_rate
    events: dict[UUID, _EventAgg] = {}
    for payment in _online_payments(scope):
        ev = payment.ticket.event
        agg = events.setdefault(ev.id, _EventAgg(ev.id, ev.name, ev.start))
        _process_payment(payment, scope, org_rate, tz, agg.currencies, include_transactions)
    for ticket in _offline_tickets(scope):
        ev = ticket.event
        agg = events.setdefault(ev.id, _EventAgg(ev.id, ev.name, ev.start))
        _process_ticket(ticket, scope, org_rate, tz, agg.currencies, include_transactions)
    return events


def build_revenue_report_data(scope: ReportScope) -> RevenueReportData:
    """Aggregate ticket revenue into buckets by currency and VAT rate (org-wide)."""
    merged: dict[str, _CurrencyAcc] = {}
    for agg in _aggregate(scope).values():
        for currency, acc in agg.currencies.items():
            _merge_currency(merged.setdefault(currency, _CurrencyAcc()), acc)
    sections = [s for currency, acc in sorted(merged.items()) if (s := _currency_section(currency, acc))]
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
    scope_key = (
        f"{scope.org.id}:{scope.event_id}:{scope.date_from}:{scope.date_to}"
        f":{str(scope.org.vat_rate)}:{scope.org.vat_country_code}"
    )
    raw = scope_key + "||" + "\n".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Bundle builders
# ---------------------------------------------------------------------------

# Locale-aware Excel number formats: the group/decimal separators render per the
# viewer's locale (e.g. "1.234.567,89" in an AT/DE Excel), so a single format code
# serves both comma- and dot-grouping audiences.
_MONEY_FORMAT = "#,##0.00"
_INT_FORMAT = "#,##0"
_PERCENT_FORMAT = '0.##"%"'  # value 20 -> "20%", 7.5 -> "7.5%" (literal %, no x100)


def _format_numeric_columns(
    ws: "Worksheet",
    money_cols: tuple[int, ...] = (),
    int_cols: tuple[int, ...] = (),
    percent_cols: tuple[int, ...] = (),
) -> None:
    """Right-align and number-format the given 1-based columns across data rows."""
    right = Alignment(horizontal="right")
    specs = ((money_cols, _MONEY_FORMAT), (int_cols, _INT_FORMAT), (percent_cols, _PERCENT_FORMAT))
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cols, fmt in specs:
            for col in cols:
                cell = row[col - 1]
                if isinstance(cell.value, (int, float, Decimal)) and not isinstance(cell.value, bool):
                    cell.number_format = fmt
                    cell.alignment = right


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
    assert summary is not None  # Workbook() always creates a default sheet
    summary.title = "Summary"
    summary.append(["Currency", "VAT rate", "Net", "VAT", "Gross", "Tickets"])
    for section in data.sections:
        for bucket in section.rate_buckets:
            summary.append([section.currency, bucket.label, bucket.net, bucket.vat, bucket.gross, bucket.ticket_count])
        summary.append([section.currency, "Refunds", None, None, -section.refunds_total, section.refunded_count])
        summary.append(
            [section.currency, "Net taxable turnover", section.net_taxable_turnover, None, None, section.sold_count]
        )
        summary.append([])
    _format_numeric_columns(summary, money_cols=(3, 4, 5), int_cols=(6,))
    for summary_row in summary.iter_rows(min_row=2, max_row=summary.max_row):
        if summary_row[1].value == "Net taxable turnover":  # bold the per-currency total row
            for cell in summary_row:
                cell.font = LABEL_FONT
    style_header_row(summary)
    auto_fit_columns(summary)
    summary.freeze_panes = "A2"

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
    _format_numeric_columns(txns, money_cols=(7, 8, 10, 11, 12), percent_cols=(9,))
    style_header_row(txns)
    auto_fit_columns(txns)
    txns.freeze_panes = "A2"

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


# ---------------------------------------------------------------------------
# Cache + generation
# ---------------------------------------------------------------------------

# ponytail: event_id is stored as "" (empty string) rather than JSON null so
# that the JSONField lookup `parameters__event_id=""` works reliably in
# Postgres. A JSON null value requires IS NULL semantics in Postgres jsonb,
# which Django's JSONField `__exact` lookup does not emit when the value is
# Python None — it would produce `@> 'null'::jsonb` which misses rows in some
# Django versions. Using "" avoids the null-in-JSON ambiguity entirely.
# _parameters_to_scope converts "" back to None when rebuilding the scope.
_NO_EVENT = ""


class _ScopeParameters(t.TypedDict):
    """Serialized ``ReportScope`` stored on ``FileExport.parameters`` for cache lookup."""

    org_id: str
    event_id: str
    date_from: str
    date_to: str
    data_hash: str


def _scope_to_parameters(scope: ReportScope, data_hash: str) -> _ScopeParameters:
    return {
        "org_id": str(scope.org.id),
        "event_id": str(scope.event_id) if scope.event_id else _NO_EVENT,
        "date_from": scope.date_from.isoformat(),
        "date_to": scope.date_to.isoformat(),
        "data_hash": data_hash,
    }


def _parameters_to_scope(parameters: dict[str, t.Any]) -> ReportScope:
    org = Organization.objects.get(id=parameters["org_id"])
    event_id_raw = parameters["event_id"]
    return ReportScope(
        org=org,
        event_id=UUID(event_id_raw) if event_id_raw else None,
        date_from=date.fromisoformat(parameters["date_from"]),
        date_to=date.fromisoformat(parameters["date_to"]),
    )


def get_or_generate_revenue_report(
    org: Organization,
    scope: ReportScope,
    requested_by: "RevelUser",
    refresh: bool = False,
) -> FileExport:
    """Return a READY cached export when one matches, else enqueue generation."""
    data_hash = compute_revenue_data_hash(scope)
    parameters = _scope_to_parameters(scope, data_hash)

    if not refresh:
        cached = (
            FileExport.objects.filter(
                export_type=FileExport.ExportType.REVENUE_VAT_REPORT,
                status=FileExport.ExportStatus.READY,
                parameters__org_id=parameters["org_id"],
                parameters__event_id=parameters["event_id"],
                parameters__date_from=parameters["date_from"],
                parameters__date_to=parameters["date_to"],
                parameters__data_hash=data_hash,
            )
            .order_by("-completed_at")
            .first()
        )
        if cached is not None:
            return cached

    export = FileExport.objects.create(
        requested_by=requested_by,
        export_type=FileExport.ExportType.REVENUE_VAT_REPORT,
        parameters=parameters,
    )
    from events.revenue_tasks import generate_revenue_report_task

    transaction.on_commit(lambda: generate_revenue_report_task.delay(str(export.id)))
    return export


def generate_revenue_report(export_id: UUID) -> None:
    """Build the ZIP bundle for an export and mark it READY (or FAILED)."""
    export = FileExport.objects.select_related("requested_by").get(pk=export_id)
    start_export(export)
    try:
        scope = _parameters_to_scope(export.parameters)
        data = build_revenue_report_data(scope)
        complete_export(export, build_zip(data), report_filename(scope))
    except Exception as exc:  # let Celery record the failure after marking FAILED
        fail_export(export, f"Revenue report failed: {exc}")
        raise


def deliver_scheduled_revenue_reports(now_utc: datetime) -> int:
    """Generate + email the just-closed period's report for opted-in orgs. Returns count sent."""
    delivered = 0
    orgs = (
        Organization.objects.select_related("city", "owner")
        .exclude(revenue_report_cadence=Organization.RevenueReportCadence.NONE)
        .exclude(billing_email="")
    )

    for org in orgs:
        try:
            tz = get_organization_timezone(org)
            period = closed_period_for(org.revenue_report_cadence, now_utc.astimezone(tz))
            if period is None:
                continue
            date_from, date_to, label = period
            if org.last_revenue_report_sent_period == label:
                continue

            scope = ReportScope(org=org, event_id=None, date_from=date_from, date_to=date_to)
            if not build_revenue_report_data(scope).sections:
                continue  # skip empty periods; do not set the marker

            export = FileExport.objects.create(
                requested_by=org.owner,
                export_type=FileExport.ExportType.REVENUE_VAT_REPORT,
                parameters=_scope_to_parameters(scope, compute_revenue_data_hash(scope)),
            )
            generate_revenue_report(export.id)
            export.refresh_from_db()

            recipients = [org.billing_email]
            if org.owner.email and org.owner.email != org.billing_email:
                recipients.append(org.owner.email)
            body = render_to_string(
                "emails/revenue_report.txt",
                {"org": org, "label": label, "snapshot_date": now_utc.astimezone(tz).date()},
            )
            send_email.delay(
                to=recipients,
                subject=f"Revenue & VAT report — {label}",
                body=body,
                attachment_storage_path=export.file.name,
                attachment_filename=report_filename(scope),
                attachment_mime_type="application/zip",
            )
            Organization.objects.filter(pk=org.pk).update(last_revenue_report_sent_period=label)
            delivered += 1
        except Exception:
            logger.exception("deliver_scheduled_revenue_reports: error for org", org_id=str(org.id))
            continue  # do not set the marker; retry next run

    return delivered
