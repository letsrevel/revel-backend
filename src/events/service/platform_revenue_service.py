"""Revel's own income: platform-fee totals and membership-fee MRR.

A fee counts as *collected* when its payment is SUCCEEDED or REFUNDED: the platform fee is never
refunded, so a payment that was collected and later refunded still counts. PENDING, FAILED and VOIDED
rows and rows with a zero fee are excluded. Series-pass purchases create ordinary ticket ``Payment`` rows,
so they are covered by the ``Payment`` source. ``PlatformFeeCreditNote`` is not subtracted.
"""

import datetime
import typing as t
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DecimalField, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from events.models import MembershipPayment, MembershipSubscription, Payment

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")
_COLLECTED = ("succeeded", "refunded")
_LIVE_SUBSCRIPTION_STATUSES = (
    MembershipSubscription.SubscriptionStatus.ACTIVE,
    MembershipSubscription.SubscriptionStatus.PAST_DUE,
)
_MONTH_SECONDS = Decimal(30 * 24 * 3600)


@dataclass
class FeeTotals:
    """Platform-fee totals for one currency.

    ``legacy_count`` is the number of rows without a net/VAT breakdown (net falls back to gross, VAT to 0).
    """

    currency: str
    gross: Decimal = _ZERO
    net: Decimal = _ZERO
    vat: Decimal = _ZERO
    legacy_count: int = 0


def local_midnight(
    now: datetime.datetime | None = None, *, days_back: int = 0, first_of_month: bool = False
) -> datetime.datetime:
    """Return local (Europe/Vienna) midnight of today minus ``days_back`` days, or of the 1st of the month."""
    day = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if first_of_month:
        day = day.replace(day=1)
    # Subtract on the naive wall-clock date so DST days keep the correct local midnight.
    return timezone.make_aware(day - datetime.timedelta(days=days_back))


def _add(totals: dict[str, FeeTotals], currency: str, gross: Decimal, net: Decimal, vat: Decimal, legacy: int) -> None:
    row = totals.setdefault(currency, FeeTotals(currency))
    row.gross += gross
    row.net += net
    row.vat += vat
    row.legacy_count += legacy


def _fee_rows(qs: QuerySet[t.Any]) -> list[dict[str, t.Any]]:
    rows: list[dict[str, t.Any]] = list(
        qs.filter(platform_fee__gt=0)
        .order_by()  # the model's default ordering would otherwise leak into GROUP BY
        .values("currency")
        .annotate(
            gross_=Sum("platform_fee"),
            net_=Sum(Coalesce("platform_fee_net", "platform_fee")),
            vat_=Sum(Coalesce("platform_fee_vat", Value(_ZERO, output_field=DecimalField()))),
            legacy_=Count("pk", filter=Q(platform_fee_net__isnull=True)),
        )
    )
    return rows


def platform_fee_totals(start: datetime.datetime, end: datetime.datetime) -> dict[str, FeeTotals]:
    """Return collected platform fees per currency for payments in ``[start, end)``.

    Ticket payments are bucketed by ``created_at`` (they have no paid timestamp, and ``updated_at`` moves on
    refund); membership payments by ``occurred_at``, falling back to ``created_at``.
    """
    ticket_qs = Payment.objects.filter(status__in=_COLLECTED, created_at__gte=start, created_at__lt=end)
    membership_qs = (
        MembershipPayment.objects.filter(status__in=_COLLECTED)
        .annotate(paid_at=Coalesce("occurred_at", "created_at"))
        .filter(paid_at__gte=start, paid_at__lt=end)
    )
    totals: dict[str, FeeTotals] = {}
    for row in [*_fee_rows(ticket_qs), *_fee_rows(membership_qs)]:
        _add(totals, row["currency"], row["gross_"], row["net_"], row["vat_"], row["legacy_"])
    return dict(sorted(totals.items()))


def membership_fee_mrr() -> dict[str, FeeTotals]:
    """Platform-fee MRR from recurring memberships, per currency, normalised to a 30-day month.

    Takes each ACTIVE or PAST_DUE subscription's most recent SUCCEEDED fee-bearing payment and scales its
    fee by ``30 days / (period_end - period_start)``. Free, lifetime and offline plans carry no fee and drop out.
    Ticket fees are not recurring and are not included.
    """
    latest = (
        MembershipPayment.objects.filter(
            subscription=OuterRef("subscription"),
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            platform_fee__gt=0,
        )
        .order_by("-period_end", "-created_at")
        .values("pk")[:1]
    )
    rows = (
        MembershipPayment.objects.filter(pk=Subquery(latest), subscription__status__in=_LIVE_SUBSCRIPTION_STATUSES)
        .order_by()
        .values("currency", "platform_fee", "platform_fee_net", "platform_fee_vat", "period_start", "period_end")
    )
    totals: dict[str, FeeTotals] = {}
    for row in rows:
        seconds = Decimal((row["period_end"] - row["period_start"]).total_seconds())
        if seconds <= 0:
            continue
        factor = _MONTH_SECONDS / seconds
        gross = row["platform_fee"]
        legacy = row["platform_fee_net"] is None
        net = gross if row["platform_fee_net"] is None else row["platform_fee_net"]
        vat = row["platform_fee_vat"] or _ZERO
        _add(totals, row["currency"], gross * factor, net * factor, vat * factor, int(legacy))
    for fee in totals.values():
        fee.gross, fee.net, fee.vat = (v.quantize(_CENT, ROUND_HALF_UP) for v in (fee.gross, fee.net, fee.vat))
    return dict(sorted(totals.items()))
