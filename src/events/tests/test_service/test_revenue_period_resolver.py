"""Tests for revenue period resolution (#551 addendum)."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from events.exceptions import InvalidPeriodError
from events.service.revenue_aggregation import resolve_period

UTC = ZoneInfo("UTC")


def test_full_year_when_only_year() -> None:
    assert resolve_period(2025, None, None, UTC, default_all_time=False) == (
        dt.date(2025, 1, 1),
        dt.date(2025, 12, 31),
    )


def test_month_window() -> None:
    assert resolve_period(2024, 2, None, UTC, default_all_time=False) == (
        dt.date(2024, 2, 1),
        dt.date(2024, 2, 29),  # leap year, last day inferred
    )


def test_quarter_window() -> None:
    assert resolve_period(2025, None, 3, UTC, default_all_time=False) == (
        dt.date(2025, 7, 1),
        dt.date(2025, 9, 30),
    )


@pytest.mark.parametrize(
    ("quarter", "expected"),
    [
        (1, (dt.date(2025, 1, 1), dt.date(2025, 3, 31))),
        (2, (dt.date(2025, 4, 1), dt.date(2025, 6, 30))),
        (3, (dt.date(2025, 7, 1), dt.date(2025, 9, 30))),
        (4, (dt.date(2025, 10, 1), dt.date(2025, 12, 31))),
    ],
)
def test_all_quarter_windows(quarter: int, expected: tuple[dt.date, dt.date]) -> None:
    assert resolve_period(2025, None, quarter, UTC, default_all_time=False) == expected


def test_default_all_time_spans_min_to_today() -> None:
    date_from, date_to = resolve_period(None, None, None, UTC, default_all_time=True)
    assert date_from == dt.date.min
    assert date_to == dt.datetime.now(UTC).date()


def test_default_current_year_when_not_all_time() -> None:
    today = dt.datetime.now(UTC).date()
    assert resolve_period(None, None, None, UTC, default_all_time=False) == (
        dt.date(today.year, 1, 1),
        dt.date(today.year, 12, 31),
    )


def test_month_and_quarter_mutually_exclusive() -> None:
    with pytest.raises(InvalidPeriodError):
        resolve_period(2025, 1, 1, UTC, default_all_time=False)
