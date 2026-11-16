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

from django.db.models import Count, DecimalField, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from events.models import MembershipPayment, MembershipSubscription, Payment

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")
COLLECTED_STATUSES = ("succeeded", "refunded")
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


def _fee_rows(qs: QuerySet[t.Any], ts: str, starts: dict[str, datetime.datetime]) -> list[dict[str, t.Any]]:
    """One grouped query computing gross/net/VAT/legacy per currency for every window start in ``starts``."""
    aggregates: dict[str, t.Any] = {}
    zero = Value(_ZERO, output_field=DecimalField())
    for key, start in starts.items():
        in_window = Q(**{f"{ts}__gte": start})
        aggregates[f"n_{key}"] = Count("pk", filter=in_window)
        aggregates[f"gross_{key}"] = Sum("platform_fee", filter=in_window)
        aggregates[f"net_{key}"] = Sum(Coalesce("platform_fee_net", "platform_fee"), filter=in_window)
        aggregates[f"vat_{key}"] = Sum(Coalesce("platform_fee_vat", zero), filter=in_window)
        aggregates[f"legacy_{key}"] = Count("pk", filter=in_window & Q(platform_fee_net__isnull=True))
    return list(
        qs.filter(platform_fee__gt=0)
        .order_by()  # the model's default ordering would otherwise leak into GROUP BY
        .values("currency")
        .annotate(**aggregates)
    )


def platform_fee_windows(
    starts: dict[str, datetime.datetime], end: datetime.datetime
) -> dict[str, dict[str, FeeTotals]]:
    """Return collected platform fees per currency for each window ``[starts[key], end)``.

    Two queries in total (one per source) regardless of the number of windows: each source is scanned once
    from the earliest start, with conditional aggregation per window. Ticket payments are bucketed by
    ``created_at`` (they have no paid timestamp, and ``updated_at`` moves on refund); membership payments by
    ``occurred_at``, falling back to ``created_at``.
    """
    lower = min(starts.values())
    ticket_qs = Payment.objects.filter(status__in=COLLECTED_STATUSES, created_at__gte=lower, created_at__lt=end)
    membership_qs = (
        MembershipPayment.objects.filter(status__in=COLLECTED_STATUSES)
        .annotate(paid_at=Coalesce("occurred_at", "created_at"))
        .filter(paid_at__gte=lower, paid_at__lt=end)
    )
    rows = [*_fee_rows(ticket_qs, "created_at", starts), *_fee_rows(membership_qs, "paid_at", starts)]
    result: dict[str, dict[str, FeeTotals]] = {key: {} for key in starts}
    for row in rows:
        for key in starts:
            if row[f"n_{key}"]:
                _add(
                    result[key],
                    row["currency"],
                    row[f"gross_{key}"],
                    row[f"net_{key}"],
                    row[f"vat_{key}"],
                    row[f"legacy_{key}"],
                )
    return {key: dict(sorted(totals.items())) for key, totals in result.items()}


def platform_fee_totals(start: datetime.datetime, end: datetime.datetime) -> dict[str, FeeTotals]:
    """Return collected platform fees per currency for payments in ``[start, end)``."""
    return platform_fee_windows({"window": start}, end)["window"]


def membership_fee_mrr() -> dict[str, FeeTotals]:
    """Platform-fee MRR from recurring memberships, per currency, normalised to a 30-day month.

    Takes each ACTIVE or PAST_DUE subscription's latest collected payment (SUCCEEDED or REFUNDED, as for fee
    totals) and scales its fee by ``30 days / (period_end - period_start)``. A latest payment without a fee
    (free, lifetime, offline) contributes 0, even if an older payment carried one. Ticket fees are not recurring
    and are not included. One query (``DISTINCT ON subscription``).
    """
    rows = (
        MembershipPayment.objects.filter(
            subscription__status__in=_LIVE_SUBSCRIPTION_STATUSES, status__in=COLLECTED_STATUSES
        )
        .order_by("subscription_id", "-period_end", "-created_at")
        .distinct("subscription_id")
        .values("currency", "platform_fee", "platform_fee_net", "platform_fee_vat", "period_start", "period_end")
    )
    totals: dict[str, FeeTotals] = {}
    for row in rows:
        seconds = Decimal((row["period_end"] - row["period_start"]).total_seconds())
        if seconds <= 0 or row["platform_fee"] <= 0:
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
