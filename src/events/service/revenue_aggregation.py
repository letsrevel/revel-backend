"""Tax-precise revenue & VAT aggregation engine (#551).

Single source of truth for every revenue view: the downloadable report rolls
this up across events, the org endpoint groups it by event, and the per-event
endpoint filters it to one event.
"""

import calendar
import copy
import hashlib
import typing as t
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.db.models import Q, QuerySet

from common.service.vat_utils import calculate_vat_inclusive
from events.models import Organization, Payment, Ticket, TicketTier
from events.utils import get_organization_timezone

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
