"""Unit tests for period arithmetic helpers."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from events.models import MembershipSubscriptionPlan
from events.utils.subscription_periods import (
    calculate_period_end,
    first_period,
    first_period_end,
    first_period_price,
)


class _PlanProxy:
    """Lightweight stand-in for MembershipSubscriptionPlan in arithmetic tests."""

    def __init__(self, period_unit: str, period_count: int) -> None:
        self.period_unit = period_unit
        self.period_count = period_count
        self.period_alignment = MembershipSubscriptionPlan.PeriodAlignment.ROLLING


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
    # Only LIFETIME returns None, and this is a MONTH plan.
    assert end is not None
    assert end.tzinfo is not None
    assert end > start


def test_calculate_period_end_rejects_unknown_unit() -> None:
    """Unsupported period units raise ``ValueError`` rather than passing silently."""
    plan = _PlanProxy("week", 1)
    with pytest.raises(ValueError):
        calculate_period_end(timezone.now(), plan)  # type: ignore[arg-type]


# ---- Calendar-aligned plans ----

UTC = datetime.timezone.utc
Alignment = MembershipSubscriptionPlan.PeriodAlignment
Pricing = MembershipSubscriptionPlan.FirstPeriodPricing
Unit = MembershipSubscriptionPlan.PeriodUnit


class _CalendarPlan(_PlanProxy):
    def __init__(
        self,
        unit: str,
        *,
        start_month: int = 1,
        pricing: str = Pricing.PRORATED,
        price: str = "120.00",
        currency: str = "EUR",
    ) -> None:
        super().__init__(unit, 1)
        self.period_alignment = Alignment.CALENDAR
        self.calendar_start_month = start_month
        self.first_period_pricing = pricing
        self.price = Decimal(price)
        self.currency = currency


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    "start, unit, start_month, expected",
    [
        (_dt(2026, 3, 15, 10, 30), Unit.MONTH, 1, _dt(2026, 4, 1)),
        # Exactly on a boundary -> the FOLLOWING boundary (renewals chain).
        (_dt(2026, 4, 1), Unit.MONTH, 1, _dt(2026, 5, 1)),
        # Year rollover.
        (_dt(2026, 12, 31, 23, 59), Unit.MONTH, 1, _dt(2027, 1, 1)),
        # Leap-year February.
        (_dt(2028, 2, 29, 12), Unit.MONTH, 1, _dt(2028, 3, 1)),
        (_dt(2026, 2, 28, 12), Unit.MONTH, 1, _dt(2026, 3, 1)),
        # Calendar year, January start.
        (_dt(2026, 6, 1), Unit.YEAR, 1, _dt(2027, 1, 1)),
        (_dt(2027, 1, 1), Unit.YEAR, 1, _dt(2028, 1, 1)),
        # September start: before and after the boundary, and exactly on it.
        (_dt(2026, 3, 10), Unit.YEAR, 9, _dt(2026, 9, 1)),
        (_dt(2026, 9, 1), Unit.YEAR, 9, _dt(2027, 9, 1)),
        (_dt(2026, 11, 20), Unit.YEAR, 9, _dt(2027, 9, 1)),
    ],
)
def test_calculate_period_end_calendar(
    start: datetime.datetime, unit: str, start_month: int, expected: datetime.datetime
) -> None:
    plan = _CalendarPlan(unit, start_month=start_month)
    assert calculate_period_end(start, plan) == expected  # type: ignore[arg-type]


def test_calculate_period_end_calendar_normalises_to_utc() -> None:
    """A non-UTC input is bucketed by its UTC instant."""
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    start = datetime.datetime(2026, 4, 1, 1, 0, tzinfo=plus_two)  # = 2026-03-31 23:00 UTC
    plan = _CalendarPlan(Unit.MONTH)
    assert calculate_period_end(start, plan) == _dt(2026, 4, 1)  # type: ignore[arg-type]


def test_calculate_period_end_calendar_lifetime_is_none() -> None:
    plan = _CalendarPlan(Unit.LIFETIME)
    assert calculate_period_end(_dt(2026, 1, 1), plan) is None  # type: ignore[arg-type]


def test_first_period_price_rolling_is_none() -> None:
    plan = _PlanProxy(Unit.MONTH, 1)
    assert first_period_price(plan, _dt(2026, 1, 1)) is None  # type: ignore[arg-type]
    assert first_period_end(plan, _dt(2026, 1, 1)) is None  # type: ignore[arg-type]


def test_first_period_price_full() -> None:
    plan = _CalendarPlan(Unit.MONTH, pricing=Pricing.FULL, price="30.00")
    assert first_period_price(plan, _dt(2026, 4, 29)) == Decimal("30.00")  # type: ignore[arg-type]


def test_first_period_price_prorated_mid_month() -> None:
    # April has 30 days; joining at noon on the 16th leaves 14.5 of 30 days.
    plan = _CalendarPlan(Unit.MONTH, price="30.00")
    assert first_period_price(plan, _dt(2026, 4, 16, 12)) == Decimal("14.50")  # type: ignore[arg-type]


def test_first_period_price_prorated_exact_boundary_is_full() -> None:
    plan = _CalendarPlan(Unit.MONTH, price="30.00")
    assert first_period_price(plan, _dt(2026, 4, 1)) == Decimal("30.00")  # type: ignore[arg-type]
    assert first_period_end(plan, _dt(2026, 4, 1)) == _dt(2026, 5, 1)  # type: ignore[arg-type]


def test_first_period_price_prorated_leap_february_and_rounding() -> None:
    # 2028 February has 29 days; 1 day left (from Feb 29 00:00) -> 10/29 = 0.3448...
    plan = _CalendarPlan(Unit.MONTH, price="10.00")
    assert first_period_price(plan, _dt(2028, 2, 29)) == Decimal("0.34")  # type: ignore[arg-type]


def test_first_period_price_prorated_year_with_september_start() -> None:
    # Sep 1 2026 -> Sep 1 2027 is 365 days; joining Mar 3 2027 leaves 182 days.
    plan = _CalendarPlan(Unit.YEAR, start_month=9, price="365.00")
    assert first_period_price(plan, _dt(2027, 3, 3)) == Decimal("182.00")  # type: ignore[arg-type]


def test_first_period_price_rounds_to_whole_units_for_zero_decimal_currency() -> None:
    # 1/29 of 1000 JPY = 34.48...: JPY has no minor unit, so a 2dp result would be unpayable.
    plan = _CalendarPlan(Unit.MONTH, price="1000", currency="JPY")
    price = first_period_price(plan, _dt(2028, 2, 29))  # type: ignore[arg-type]
    assert price == Decimal("34")
    assert price is not None and price.as_tuple().exponent == 0


def test_first_period_price_full_rounds_to_currency_minor_unit() -> None:
    # A FULL first period must be as payable as a prorated one: JPY has no minor unit.
    plan = _CalendarPlan(Unit.MONTH, pricing=Pricing.FULL, price="1000.50", currency="JPY")
    price = first_period_price(plan, _dt(2028, 2, 29))  # type: ignore[arg-type]
    assert price is not None and price.as_tuple().exponent == 0


def test_first_period_returns_end_and_price_together() -> None:
    plan = _CalendarPlan(Unit.MONTH, price="30.00")
    assert first_period(plan, _dt(2026, 4, 16, 12)) == (_dt(2026, 5, 1), Decimal("14.50"))  # type: ignore[arg-type]
    assert first_period(_PlanProxy(Unit.MONTH, 1), _dt(2026, 4, 16)) == (None, None)  # type: ignore[arg-type]
