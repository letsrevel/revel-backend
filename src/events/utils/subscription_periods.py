"""Period arithmetic helpers for membership subscriptions.

Uses :class:`dateutil.relativedelta.relativedelta` so that month-end edges
(e.g. Jan 31 + 1 month) collapse to the next month's last day instead of
spilling forward or raising.
"""

import datetime
import typing as t

from dateutil.relativedelta import relativedelta

if t.TYPE_CHECKING:
    from events.models import MembershipSubscriptionPlan


def calculate_period_end(
    period_start: datetime.datetime,
    plan: "MembershipSubscriptionPlan",
) -> datetime.datetime:
    """Return the period end for a renewal anchored at ``period_start``.

    Args:
        period_start: Start of the billing period (timezone-aware).
        plan: The :class:`MembershipSubscriptionPlan` whose cadence is used.

    Returns:
        ``period_start + plan.period_count * plan.period_unit`` with month-end
        edges handled by :class:`relativedelta`.
    """
    delta = relativedelta(**{f"{plan.period_unit}s": plan.period_count})  # type: ignore[arg-type]
    return period_start + delta
