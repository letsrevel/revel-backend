"""Period arithmetic helpers for membership subscriptions.

Uses :class:`dateutil.relativedelta.relativedelta` so that month-end edges
(e.g. Jan 31 + 1 month) collapse to the next month's last day instead of
spilling forward or raising.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from events.models import MembershipSubscriptionPlan
from events.utils.currency import round_major_amount

# Days before period_end at which the renewal reminder notification fires.
REMINDER_DAYS = 3


def _is_calendar(plan: MembershipSubscriptionPlan) -> bool:
    return plan.period_alignment == MembershipSubscriptionPlan.PeriodAlignment.CALENDAR


def _calendar_boundaries(
    at: datetime.datetime, plan: MembershipSubscriptionPlan
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return ``(previous, next)`` calendar boundaries around ``at``, at 00:00 UTC.

    ``previous`` is the latest boundary at or before ``at``; ``next`` the first one strictly after it.
    """
    at = at.astimezone(datetime.timezone.utc)
    if plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.MONTH:
        previous = datetime.datetime(at.year, at.month, 1, tzinfo=datetime.timezone.utc)
        return previous, previous + relativedelta(months=1)
    if plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.YEAR:
        start_month = plan.calendar_start_month
        year = at.year if at.month >= start_month else at.year - 1
        previous = datetime.datetime(year, start_month, 1, tzinfo=datetime.timezone.utc)
        return previous, previous + relativedelta(years=1)
    raise ValueError(f"Unsupported calendar period unit: {plan.period_unit!r}")


def first_period_end(plan: MembershipSubscriptionPlan, at: datetime.datetime) -> datetime.datetime | None:
    """End of the first period for a member joining at ``at``; ``None`` unless the plan is calendar-aligned."""
    return _calendar_boundaries(at, plan)[1] if _is_calendar(plan) else None


def starts_mid_period(plan: MembershipSubscriptionPlan, at: datetime.datetime) -> bool:
    """True when ``at`` is not exactly a calendar boundary of ``plan``, so a period starting there is a stub."""
    return _is_calendar(plan) and _calendar_boundaries(at, plan)[0] != at.astimezone(datetime.timezone.utc)


def first_period_price(plan: MembershipSubscriptionPlan, at: datetime.datetime) -> Decimal | None:
    """Price of the first period for a member joining at ``at``.

    Returns:
        ``None`` for rolling plans (``first_period_pricing`` does not apply),
        ``plan.price`` for FULL pricing, else ``plan.price`` scaled by the share of
        the calendar period still remaining at ``at``. Joining exactly on a
        boundary leaves the whole period, so the price is the full price.
    """
    if not _is_calendar(plan):
        return None
    if plan.first_period_pricing == MembershipSubscriptionPlan.FirstPeriodPricing.FULL:
        return round_major_amount(plan.price, plan.currency)
    previous, following = _calendar_boundaries(at, plan)
    remaining = (following - at).total_seconds()
    total = (following - previous).total_seconds()
    return round_major_amount(plan.price * Decimal(str(remaining)) / Decimal(str(total)), plan.currency)


def first_period(
    plan: MembershipSubscriptionPlan, at: datetime.datetime
) -> tuple[datetime.datetime | None, Decimal | None]:
    """``(end, price)`` of the first period for a member joining at ``at``; both ``None`` for rolling plans."""
    return first_period_end(plan, at), first_period_price(plan, at)


def calculate_period_end(
    period_start: datetime.datetime,
    plan: MembershipSubscriptionPlan,
) -> datetime.datetime | None:
    """Return the period end for a renewal anchored at ``period_start``.

    Args:
        period_start: Start of the billing period (timezone-aware).
        plan: The :class:`MembershipSubscriptionPlan` whose cadence is used.

    Returns:
        For a calendar plan, the next 00:00 UTC boundary strictly after
        ``period_start`` (1st of next month, or next 1st of
        ``calendar_start_month``). Otherwise
        ``period_start + plan.period_count * plan.period_unit`` with month-end
        edges handled by :class:`relativedelta`, or ``None`` for a LIFETIME
        plan — it never renews, so its ``current_period_end`` stays NULL and
        the lapse/reminder/expiry beats (which all filter on that column) never
        select it.

    Raises:
        ValueError: If ``plan.period_unit`` is not a known unit.
    """
    if plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.LIFETIME:
        return None
    if _is_calendar(plan):
        # Strictly after ``period_start``: a start sitting on a boundary (renewal
        # chaining from the previous period end) yields the following boundary.
        return _calendar_boundaries(period_start, plan)[1]
    if plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.MONTH:
        delta = relativedelta(months=plan.period_count)
    elif plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.YEAR:
        delta = relativedelta(years=plan.period_count)
    else:
        raise ValueError(f"Unsupported subscription period unit: {plan.period_unit!r}")
    return period_start + delta
