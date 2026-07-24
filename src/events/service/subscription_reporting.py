"""Per-organization subscription reporting (MRR, churn, status breakdown).

See: docs/superpowers/specs/2026-05-12-subscriptions-phase-4-design.md §9
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q
from django.utils import timezone

from events.models import MembershipSubscription, MembershipSubscriptionPlan, Organization


class StatusBreakdown(t.TypedDict):
    pending: int
    active: int
    paused: int
    past_due: int
    cancelled: int
    expired: int


class SubscriptionMetrics(t.TypedDict):
    as_of: t.Any  # timezone-aware datetime — typed as Any to keep TypedDict friendly
    active_count: int
    mrr: Decimal
    mrr_currency: str
    mixed_currency_warning: bool
    new_subscribers_30d: int
    churned_30d: int
    churn_rate_30d: float
    status_breakdown: StatusBreakdown


_NON_TERMINAL_STATUSES: list[str] = [
    MembershipSubscription.SubscriptionStatus.PENDING,
    MembershipSubscription.SubscriptionStatus.ACTIVE,
    MembershipSubscription.SubscriptionStatus.PAUSED,
    MembershipSubscription.SubscriptionStatus.PAST_DUE,
]


def get_organization_metrics(organization: Organization) -> SubscriptionMetrics:
    """Compute subscription metrics for an organization.

    Args:
        organization: The organization to compute metrics for.

    Returns:
        A :class:`SubscriptionMetrics` dict with MRR, churn, and status breakdown.
    """
    now = timezone.now()
    cutoff = now - timedelta(days=30)
    statuses = MembershipSubscription.SubscriptionStatus

    # Single aggregate for status breakdown
    breakdown_raw = MembershipSubscription.objects.filter(organization=organization).aggregate(
        pending=Count("id", filter=Q(status=statuses.PENDING)),
        active=Count("id", filter=Q(status=statuses.ACTIVE)),
        paused=Count("id", filter=Q(status=statuses.PAUSED)),
        past_due=Count("id", filter=Q(status=statuses.PAST_DUE)),
        cancelled=Count("id", filter=Q(status=statuses.CANCELLED)),
        expired=Count("id", filter=Q(status=statuses.EXPIRED)),
    )
    breakdown: StatusBreakdown = {
        "pending": int(breakdown_raw["pending"]),
        "active": int(breakdown_raw["active"]),
        "paused": int(breakdown_raw["paused"]),
        "past_due": int(breakdown_raw["past_due"]),
        "cancelled": int(breakdown_raw["cancelled"]),
        "expired": int(breakdown_raw["expired"]),
    }

    # ACTIVE + PAST_DUE count as "still paying customers"
    active_count = breakdown["active"] + breakdown["past_due"]

    # MRR: walk active/past_due subs joined with plans
    active_subs = MembershipSubscription.objects.filter(
        organization=organization,
        status__in=[statuses.ACTIVE, statuses.PAST_DUE],
    ).select_related("plan")

    currencies: set[str] = set()
    mrr_total = Decimal("0")
    for sub in active_subs:
        currencies.add(sub.plan.currency)
        mrr_total += _monthly_equivalent(sub.plan)

    mixed_currency_warning = len(currencies) > 1
    if mixed_currency_warning:
        mrr_currency = "MIXED"
        mrr = Decimal("0")
    elif currencies:
        mrr_currency = next(iter(currencies))
        mrr = mrr_total.quantize(Decimal("0.01"))
    else:
        mrr_currency = ""
        mrr = Decimal("0")

    new_subscribers_30d = MembershipSubscription.objects.filter(
        organization=organization,
        created_at__gte=cutoff,
        status__in=_NON_TERMINAL_STATUSES,
    ).count()

    churned_30d = (
        MembershipSubscription.objects.filter(
            organization=organization,
            status__in=[statuses.CANCELLED, statuses.EXPIRED],
        )
        .filter(Q(cancelled_at__gte=cutoff) | Q(expired_at__gte=cutoff))
        .count()
    )

    churn_denominator = active_count + churned_30d
    churn_rate_30d = (churned_30d / churn_denominator) if churn_denominator else 0.0

    return {
        "as_of": now,
        "active_count": active_count,
        "mrr": mrr,
        "mrr_currency": mrr_currency,
        "mixed_currency_warning": mixed_currency_warning,
        "new_subscribers_30d": new_subscribers_30d,
        "churned_30d": churned_30d,
        "churn_rate_30d": churn_rate_30d,
        "status_breakdown": breakdown,
    }


def _monthly_equivalent(plan: MembershipSubscriptionPlan) -> Decimal:
    """Return one subscription's monthly recurring revenue contribution (unrounded).

    Annual plans are divided by (period_count * 12) months; monthly plans
    by period_count. The returned value is unrounded; the caller is responsible
    for quantizing the running sum once to avoid accumulated rounding errors.

    Args:
        plan: The :class:`MembershipSubscriptionPlan` to normalise.

    Returns:
        The monthly equivalent price as a :class:`Decimal` (unrounded).
    """
    if plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.MONTH:
        return plan.price / plan.period_count
    # YEAR
    return plan.price / (plan.period_count * 12)
