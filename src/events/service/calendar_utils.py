"""Calendar and location utilities for events."""

import typing as t
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db.models import QuerySet
from django.utils import timezone

from events.models.mixins import LocationMixin

T = t.TypeVar("T", bound=LocationMixin)


def calculate_calendar_date_range(
    *,
    week: int | None = None,
    month: int | None = None,
    year: int | None = None,
) -> tuple[datetime, datetime]:
    """Calculate start and end datetime for calendar views.

    Args:
        week: ISO week number (1-53), uses current year if year not provided
        month: Month number (1-12), uses current year if year not provided
        year: Year (e.g., 2025)

    Returns:
        Tuple of (start_datetime, end_datetime) representing the time range.
        If no parameters provided, returns current month range.

    Priority: week > month > year > current_month
    """
    now = timezone.now()
    utc = ZoneInfo("UTC")

    if week is not None:
        # ISO week: Week containing first Thursday = Week 1
        # Jan 4 is always in Week 1 (anchor point for calculation)
        target_year = year or now.year
        jan_4 = datetime(target_year, 1, 4, tzinfo=utc)
        week_1_start = jan_4 - timedelta(days=jan_4.isoweekday() - 1)
        start_datetime = week_1_start + timedelta(weeks=week - 1)
        end_datetime = start_datetime + timedelta(weeks=1)
        return start_datetime, end_datetime

    if month is not None:
        target_year = year or now.year
        start_datetime = datetime(target_year, month, 1, tzinfo=utc)
        next_month_year = target_year + 1 if month == 12 else target_year
        next_month = 1 if month == 12 else month + 1
        end_datetime = datetime(next_month_year, next_month, 1, tzinfo=utc)
        return start_datetime, end_datetime

    if year is not None:
        start_datetime = datetime(year, 1, 1, tzinfo=utc)
        end_datetime = datetime(year + 1, 1, 1, tzinfo=utc)
        return start_datetime, end_datetime

    # Default: current month
    start_datetime = datetime(now.year, now.month, 1, tzinfo=utc)
    next_month_year = now.year + 1 if now.month == 12 else now.year
    next_month = 1 if now.month == 12 else now.month + 1
    end_datetime = datetime(next_month_year, next_month, 1, tzinfo=utc)
    return start_datetime, end_datetime


def order_by_distance(point: Point | None, queryset: QuerySet[T]) -> QuerySet[T]:
    """Order a queryset by distance from a point.

    Args:
        point: The reference point to measure distance from.
        queryset: A queryset of models with a location field.

    Returns:
        The queryset ordered by distance from the point.
    """
    if point is None:
        return queryset

    return queryset.annotate(  # type: ignore[no-any-return]
        distance=Distance("location", point),
    ).order_by("distance")
