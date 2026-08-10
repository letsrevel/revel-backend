"""Tax-precise revenue & VAT aggregation engine (#551).

Single source of truth for every revenue view: the downloadable report rolls
this up across events, the org endpoint groups it by event, and the per-event
endpoint filters it to one event.
"""

import calendar
import copy
import hashlib
import typing as t
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

if t.TYPE_CHECKING:
    from events.models import Event

from django.db.models import Q, QuerySet
from django.utils import timezone

from common.service.vat_utils import calculate_vat_inclusive
from events.models import MembershipPayment, Organization, Payment, Refund, Ticket, TicketTier
from events.service.seating.pricing import recorded_or_resolved_price
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
class MembershipTxnRow:
    """A single membership payment line for the report's detail sheet."""

    date: date
    payment_id: str
    member_email: str
    member_name: str
    plan: str
    gross: Decimal
    currency: str
    status: str
    refund_amount: Decimal
    stripe_invoice_id: str
    stripe_payment_intent_id: str


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
    """Full aggregated report data returned to callers.

    ``memberships``/``membership_payments`` are the org-level subscription totals
    and ledger for the period — kept beside (never folded into) the ticket
    ``sections``, whose VAT buckets are ticket-specific. Both are always empty for
    an event-scoped report.
    """

    scope: ReportScope
    sections: list[CurrencySection]
    generated_at: datetime
    membership_payments: list[MembershipTxnRow] = field(default_factory=list)
    memberships: list["MembershipFinancials"] = field(default_factory=list)


def resolve_period(
    year: int | None,
    month: int | None,
    quarter: int | None,
    tz: ZoneInfo,
    *,
    default_all_time: bool,
) -> tuple[date, date]:
    """Resolve (year, month, quarter) selectors into an inclusive date window.

    ``month`` and ``quarter`` are mutually exclusive. With no selectors:
    all-time (``date.min``..today) when ``default_all_time`` else the current year.

    Args:
        year: Optional calendar year (e.g. 2025).
        month: Optional month number (1–12). Mutually exclusive with ``quarter``.
        quarter: Optional quarter number (1–4). Mutually exclusive with ``month``.
        tz: Timezone to use for resolving "today".
        default_all_time: When ``True`` and no selectors are given, return
            ``(date.min, today)``; when ``False``, return the current year span.

    Returns:
        Inclusive ``(date_from, date_to)`` window.

    Raises:
        InvalidPeriodError: When both ``month`` and ``quarter`` are specified.
    """
    from events.exceptions import InvalidPeriodError

    if month is not None and quarter is not None:
        raise InvalidPeriodError("Specify either month or quarter, not both.")
    today = datetime.now(tz).date()
    if year is None and month is None and quarter is None and default_all_time:
        return date.min, today
    year = year if year is not None else today.year
    if month is not None:
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)
    if quarter is not None:
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        last_day = calendar.monthrange(year, end_month)[1]
        return date(year, start_month, 1), date(year, end_month, last_day)
    return date(year, 1, 1), date(year, 12, 31)


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
    # ``refunds`` feeds the per-row refund attribution in _process_payment —
    # prefetched here so the pass stays N+1-free.
    qs = (
        Payment.objects.select_related("ticket__event", "ticket__tier")
        .prefetch_related("refunds")
        .filter(
            ticket__event__organization=scope.org,
            ticket__tier__payment_method=TicketTier.PaymentMethod.ONLINE,
            status__in=[Payment.PaymentStatus.SUCCEEDED, Payment.PaymentStatus.REFUNDED],
        )
    )
    if scope.event_id is not None:
        qs = qs.filter(ticket__event_id=scope.event_id)
    return qs


def _offline_tickets(scope: ReportScope) -> QuerySet[Ticket]:
    from events.service.ticket_service import offline_paid_q

    offline_paid = offline_paid_q()
    # ``seat`` feeds the per-seat price resolution in _process_ticket when ``price_paid``
    # is NULL on a category-priced tier — joined here so the pass stays N+1-free.
    qs = Ticket.objects.select_related("event", "tier", "seat").filter(
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
    # Refunds are attributed per Refund row, not via Payment's denormalized
    # running total (#865): ``payment.refund_amount`` accumulates across partials
    # and ``payment.refunded_at`` is overwritten by each one, which would migrate
    # an earlier period's refund into the latest refund's period on regeneration.
    # ``succeeded_at`` is the webhook's confirmation stamp; rows predating the
    # field fall back to ``updated_at`` (last webhook touch).
    in_period_refunds = [
        r
        for r in payment.refunds.all()
        if r.status == Refund.RefundStatus.SUCCEEDED
        and _in_period(_local_date(r.succeeded_at or r.updated_at, tz), scope)
    ]
    refund_total = sum((r.amount for r in in_period_refunds), ZERO)
    refund_in = bool(in_period_refunds)

    if sale_in:
        _add_sale(acc, net, vat, payment.amount, rate, rc)
    if refund_in:
        _add_refund(acc, refund_total, rate, rc)

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
                refund_amount=refund_total if refund_in else ZERO,
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
    # ``tier``/``seat`` are joined by _offline_tickets, so this stays one query for the batch.
    gross = recorded_or_resolved_price(ticket.tier, ticket.seat, ticket.price_paid)
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


class _MembershipAcc:
    """Mutable per-currency accumulator for membership subscription payments."""

    def __init__(self) -> None:
        self.gross = ZERO
        self.platform_fee = ZERO
        self.refunded = ZERO
        self.payment_count = 0
        self.transactions: list[MembershipTxnRow] = []


def _membership_payments(scope: ReportScope) -> QuerySet[MembershipPayment]:
    """Settled membership payments for the org — empty for an event-scoped report.

    Membership money belongs to the organization, not to any single event, so a
    per-event report must not claim it.
    """
    if scope.event_id is not None:
        return MembershipPayment.objects.none()
    return MembershipPayment.objects.select_related("subscription__user", "subscription__plan").filter(
        subscription__organization=scope.org,
        status__in=[MembershipPayment.PaymentStatus.SUCCEEDED, MembershipPayment.PaymentStatus.REFUNDED],
    )


def _aggregate_memberships(scope: ReportScope, *, include_transactions: bool = True) -> dict[str, _MembershipAcc]:
    """Single pass over membership payments; returns accumulators keyed by currency."""
    tz = organization_timezone(scope.org)
    currencies: dict[str, _MembershipAcc] = {}
    for payment in _membership_payments(scope):
        # ``occurred_at`` is the real hand-over date for backfilled rows (see the
        # model's help_text); ``created_at`` is the fallback, as on the ticket side.
        sale_date = _local_date(payment.occurred_at or payment.created_at, tz)
        refund_date = _local_date(payment.refunded_at, tz) if payment.refunded_at is not None else None
        sale_in = _in_period(sale_date, scope)
        refund_in = bool(payment.refund_amount and refund_date is not None and _in_period(refund_date, scope))
        if not (sale_in or refund_in):
            continue
        acc = currencies.setdefault(payment.currency, _MembershipAcc())
        if sale_in:
            acc.gross += payment.amount
            acc.platform_fee += payment.platform_fee
            acc.payment_count += 1
        if refund_in and payment.refund_amount:
            acc.refunded += payment.refund_amount
        if include_transactions:
            subscription = payment.subscription
            acc.transactions.append(
                MembershipTxnRow(
                    # A recurring subscription routinely straddles a period boundary. When the
                    # row is in scope only through its refund, stamping it with the (out-of-period)
                    # sale date puts the line outside the very period it is reported in — so a
                    # refund-only row carries the refund date instead. Both-in-period keeps the
                    # sale date, matching where the money is attributed above.
                    date=sale_date if sale_in or refund_date is None else refund_date,
                    payment_id=str(payment.id),
                    member_email=subscription.user.email,
                    member_name=subscription.user.get_display_name(),
                    plan=subscription.plan.name,
                    gross=payment.amount,
                    currency=payment.currency,
                    status=payment.status,
                    refund_amount=(payment.refund_amount or ZERO) if refund_in else ZERO,
                    stripe_invoice_id=payment.stripe_invoice_id,
                    stripe_payment_intent_id=payment.stripe_payment_intent_id,
                )
            )
    return currencies


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
    """Build a report ``CurrencySection`` or ``None`` if empty.

    Rate-bucket money (``net``/``vat``/``gross``) is net-of-refunds; ``ticket_count``
    is the gross sold count (refunds are reported separately in ``refunded_count``).
    """
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
    """Aggregate ticket revenue by currency and VAT rate, plus the membership ledger (org-wide)."""
    merged: dict[str, _CurrencyAcc] = {}
    for agg in _aggregate(scope).values():
        for currency, acc in agg.currencies.items():
            _merge_currency(merged.setdefault(currency, _CurrencyAcc()), acc)
    sections = [s for currency, acc in sorted(merged.items()) if (s := _currency_section(currency, acc))]
    membership_accs = sorted(_aggregate_memberships(scope).items())
    membership_rows = sorted(
        (row for _, acc in membership_accs for row in acc.transactions),
        key=lambda r: r.date,
    )
    return RevenueReportData(
        scope=scope,
        sections=sections,
        generated_at=timezone.now(),
        membership_payments=membership_rows,
        memberships=[_membership_financials(cur, acc) for cur, acc in membership_accs],
    )


def compute_revenue_data_hash(scope: ReportScope) -> str:
    """SHA-256 over in-scope payment, offline-ticket and membership rows for cache invalidation."""
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
    for membership_payment in _membership_payments(scope).order_by("id"):
        parts.append(
            "|".join(
                [
                    f"membership:{membership_payment.id}",
                    membership_payment.updated_at.isoformat(),
                    membership_payment.status,
                    str(membership_payment.refund_amount),
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
# Live-endpoint projections (Task 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrencyFinancials:
    """Per-currency financials for the live endpoints.

    Top-level ``gross`` is pre-refund (total ever charged) with ``refunds`` reported
    separately and ``net = gross - refunds``. The nested ``rate_buckets`` instead carry
    net-of-refunds money (``gross``/``net``/``vat`` = sale minus refund per rate).
    """

    currency: str
    gross: Decimal
    refunds: Decimal
    net: Decimal
    net_taxable: Decimal
    vat: Decimal
    sold_count: int
    refunded_count: int
    rate_buckets: list[RateBucket]


@dataclass(frozen=True)
class EventFinancials:
    """Financials for a single event, broken down by currency."""

    event_id: UUID
    event_name: str
    event_start: datetime
    by_currency: list[CurrencyFinancials]


@dataclass(frozen=True)
class MembershipFinancials:
    """Per-currency membership subscription totals for the live endpoints.

    ``net`` follows the ticket-side convention — gross minus refunds, with the
    platform fee reported separately rather than deducted — so ticket and
    membership money are addable in :class:`CombinedTotals`.
    """

    currency: str
    gross: Decimal
    platform_fee: Decimal
    net: Decimal
    payment_count: int
    refunded_amount: Decimal


@dataclass(frozen=True)
class CombinedTotals:
    """Per-currency grand total: ticket net plus membership net."""

    currency: str
    tickets_net: Decimal
    memberships_net: Decimal
    net: Decimal


@dataclass(frozen=True)
class OrganizationFinancials:
    """Org-wide financials broken down by event, scoped to an active currency.

    ``totals``/``events`` are ticket money; ``memberships`` is org-level
    subscription money kept alongside it (memberships belong to no event and
    carry no ticket VAT buckets). ``combined_totals`` adds the two per currency.
    """

    date_from: date
    date_to: date
    active_currency: str | None
    available_currencies: list[str]
    totals: list[CurrencyFinancials]
    events: list[EventFinancials]
    memberships: list[MembershipFinancials]
    combined_totals: list[CombinedTotals]


def _currency_financials(currency: str, acc: _CurrencyAcc) -> CurrencyFinancials | None:
    """Build per-currency financials, or ``None`` if no activity.

    Top-level ``gross`` is pre-refund; nested ``rate_buckets`` are net-of-refunds.
    """
    if not acc.buckets:
        return None
    rate_buckets = [
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
    gross = sum((b.sale_gross for b in acc.buckets.values()), ZERO)
    refunds = sum((b.refund_gross for b in acc.buckets.values()), ZERO)
    return CurrencyFinancials(
        currency=currency,
        gross=gross,
        refunds=refunds,
        net=gross - refunds,
        net_taxable=sum((rb.net for rb in rate_buckets), ZERO),
        vat=sum((rb.vat for rb in rate_buckets), ZERO),
        sold_count=sum(b.sold_count for b in acc.buckets.values()),
        refunded_count=sum(b.refunded_count for b in acc.buckets.values()),
        rate_buckets=rate_buckets,
    )


def _membership_financials(currency: str, acc: _MembershipAcc) -> MembershipFinancials:
    """Shape one currency's membership accumulator for the API."""
    return MembershipFinancials(
        currency=currency,
        gross=acc.gross,
        platform_fee=acc.platform_fee,
        net=acc.gross - acc.refunded,
        payment_count=acc.payment_count,
        refunded_amount=acc.refunded,
    )


def _combined_totals(
    currencies: list[str],
    tickets: list[CurrencyFinancials],
    memberships: list[MembershipFinancials],
) -> list[CombinedTotals]:
    """Ticket net + membership net per currency, for every currency with activity."""
    ticket_net = {cf.currency: cf.net for cf in tickets}
    membership_net = {mf.currency: mf.net for mf in memberships}
    return [
        CombinedTotals(
            currency=cur,
            tickets_net=ticket_net.get(cur, ZERO),
            memberships_net=membership_net.get(cur, ZERO),
            net=ticket_net.get(cur, ZERO) + membership_net.get(cur, ZERO),
        )
        for cur in currencies
    ]


def _event_financials(agg: _EventAgg) -> EventFinancials:
    by_currency = [
        cf for currency, acc in sorted(agg.currencies.items()) if (cf := _currency_financials(currency, acc))
    ]
    return EventFinancials(
        event_id=agg.event_id,
        event_name=agg.name,
        event_start=agg.start,
        by_currency=by_currency,
    )


def event_financials(event: "Event", scope: ReportScope) -> EventFinancials:
    """Per-event projection: aggregate just this event and shape it for the API."""
    agg = _aggregate(scope, include_transactions=False).get(event.id)
    if agg is None:
        return EventFinancials(event_id=event.id, event_name=event.name, event_start=event.start, by_currency=[])
    return _event_financials(agg)


def organization_financials(
    scope: ReportScope,
    *,
    currency: str | None,
    sort: str,
    order: str,
) -> OrganizationFinancials:
    """Org-wide projection grouped by event, scoped/sorted for the dashboard."""
    events_agg = _aggregate(scope, include_transactions=False)
    event_fins = [ef for agg in events_agg.values() if (ef := _event_financials(agg)).by_currency]

    # Org-wide per-currency totals (roll up across events).
    merged: dict[str, _CurrencyAcc] = {}
    for agg in events_agg.values():
        for cur, acc in agg.currencies.items():
            _merge_currency(merged.setdefault(cur, _CurrencyAcc()), acc)
    totals_all = [cf for cur, acc in sorted(merged.items()) if (cf := _currency_financials(cur, acc))]

    memberships_all = [
        _membership_financials(cur, acc)
        for cur, acc in sorted(_aggregate_memberships(scope, include_transactions=False).items())
    ]
    # A membership-only currency must still show up in the switcher and totals.
    available = sorted({cf.currency for cf in totals_all} | {mf.currency for mf in memberships_all})
    combined_all = _combined_totals(available, totals_all, memberships_all)

    # Dominant currency = highest gross (pre-refund), tickets and memberships together.
    gross_by_currency = dict.fromkeys(available, ZERO)
    for cf in totals_all:
        gross_by_currency[cf.currency] += cf.gross
    for mf in memberships_all:
        gross_by_currency[mf.currency] += mf.gross
    active = (
        currency
        if currency is not None
        else (max(available, key=lambda c: gross_by_currency[c]) if available else None)
    )

    def _net_in(ef: EventFinancials, cur: str | None) -> Decimal:
        return next((c.net for c in ef.by_currency if c.currency == cur), ZERO)

    if currency is not None:
        event_fins = [
            EventFinancials(
                ef.event_id, ef.event_name, ef.event_start, [c for c in ef.by_currency if c.currency == currency]
            )
            for ef in event_fins
        ]
        event_fins = [ef for ef in event_fins if ef.by_currency]
        totals = [c for c in totals_all if c.currency == currency]
        memberships = [m for m in memberships_all if m.currency == currency]
        combined = [c for c in combined_all if c.currency == currency]
    else:
        totals = totals_all
        memberships = memberships_all
        combined = combined_all

    reverse = order == "desc"
    if sort == "event_start":
        event_fins.sort(key=lambda ef: ef.event_start, reverse=reverse)
    else:  # "revenue"
        event_fins.sort(key=lambda ef: _net_in(ef, active), reverse=reverse)

    return OrganizationFinancials(
        date_from=scope.date_from,
        date_to=scope.date_to,
        active_currency=active,
        available_currencies=available,
        totals=totals,
        events=event_fins,
        memberships=memberships,
        combined_totals=combined,
    )
