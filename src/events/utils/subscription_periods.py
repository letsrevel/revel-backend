"""Period arithmetic helpers for membership subscriptions.

Uses :class:`dateutil.relativedelta.relativedelta` so that month-end edges
(e.g. Jan 31 + 1 month) collapse to the next month's last day instead of
spilling forward or raising.
"""

import datetime

from dateutil.relativedelta import relativedelta

from events.models import MembershipSubscriptionPlan

# Days before period_end at which the renewal reminder notification fires.
REMINDER_DAYS = 3


def calculate_period_end(
    period_start: datetime.datetime,
    plan: MembershipSubscriptionPlan,
) -> datetime.datetime | None:
    """Return the period end for a renewal anchored at ``period_start``.

    Args:
        period_start: Start of the billing period (timezone-aware).
        plan: The :class:`MembershipSubscriptionPlan` whose cadence is used.

    Returns:
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
    if plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.MONTH:
        delta = relativedelta(months=plan.period_count)
    elif plan.period_unit == MembershipSubscriptionPlan.PeriodUnit.YEAR:
        delta = relativedelta(years=plan.period_count)
    else:
        raise ValueError(f"Unsupported subscription period unit: {plan.period_unit!r}")
    return period_start + delta
