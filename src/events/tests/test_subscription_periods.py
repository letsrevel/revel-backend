"""Unit tests for period arithmetic helpers."""

import datetime

import pytest
from django.utils import timezone

from events.models import MembershipSubscriptionPlan
from events.utils.subscription_periods import calculate_period_end


class _PlanProxy:
    """Lightweight stand-in for MembershipSubscriptionPlan in arithmetic tests."""

    def __init__(self, period_unit: str, period_count: int) -> None:
        self.period_unit = period_unit
        self.period_count = period_count


@pytest.mark.parametrize(
    "start, unit, count, expected",
    [
        # Simple monthly forward
        (
            datetime.datetime(2026, 1, 15, 10, 0, tzinfo=datetime.timezone.utc),
            MembershipSubscriptionPlan.PeriodUnit.MONTH.value,
            1,
            datetime.datetime(2026, 2, 15, 10, 0, tzinfo=datetime.timezone.utc),
        ),
        # Jan 31 + 1 month collapses to Feb 28 (non-leap year)
        (
            datetime.datetime(2026, 1, 31, 12, 0, tzinfo=datetime.timezone.utc),
            MembershipSubscriptionPlan.PeriodUnit.MONTH.value,
            1,
            datetime.datetime(2026, 2, 28, 12, 0, tzinfo=datetime.timezone.utc),
        ),
        # Jan 31 + 1 month collapses to Feb 29 (leap year 2028)
        (
            datetime.datetime(2028, 1, 31, 12, 0, tzinfo=datetime.timezone.utc),
            MembershipSubscriptionPlan.PeriodUnit.MONTH.value,
            1,
            datetime.datetime(2028, 2, 29, 12, 0, tzinfo=datetime.timezone.utc),
        ),
        # 3-month plan
        (
            datetime.datetime(2026, 3, 10, 0, 0, tzinfo=datetime.timezone.utc),
            MembershipSubscriptionPlan.PeriodUnit.MONTH.value,
            3,
            datetime.datetime(2026, 6, 10, 0, 0, tzinfo=datetime.timezone.utc),
        ),
        # Annual — Feb 29 in a leap year + 1 year collapses to Feb 28.
        (
            datetime.datetime(2028, 2, 29, 0, 0, tzinfo=datetime.timezone.utc),
            MembershipSubscriptionPlan.PeriodUnit.YEAR.value,
            1,
            datetime.datetime(2029, 2, 28, 0, 0, tzinfo=datetime.timezone.utc),
        ),
    ],
)
def test_calculate_period_end(start: datetime.datetime, unit: str, count: int, expected: datetime.datetime) -> None:
    """Period arithmetic should match the parametrized table for month-end edges."""
    plan = _PlanProxy(unit, count)
    assert calculate_period_end(start, plan) == expected  # type: ignore[arg-type]


def test_calculate_period_end_uses_timezone_aware_input() -> None:
    """The helper must preserve timezone-aware semantics."""
    plan = _PlanProxy(MembershipSubscriptionPlan.PeriodUnit.MONTH.value, 1)
    start = timezone.now()
    end = calculate_period_end(start, plan)  # type: ignore[arg-type]
    assert end.tzinfo is not None
    assert end > start
