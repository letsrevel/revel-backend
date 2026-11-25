"""Calendar and location utilities for events."""

import typing as t
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.db.models import QuerySet
from django.utils import timezone

from events.models import Event
from events.models.mixins import LocationMixin
from events.utils import get_event_timezone

T = t.TypeVar("T", bound=LocationMixin)

# UTC offsets in use run from -12 to +14 hours, so an event whose local start date is in a period starts at most
# 14 hours outside the period's UTC bounds (#1520).
CALENDAR_EDGE_PADDING = timedelta(hours=14)


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


def filter_to_calendar_period(
    queryset: QuerySet[Event],
    *,
    week: int | None = None,
    month: int | None = None,
    year: int | None = None,
) -> list[Event]:
    """Return the events whose start date, in the event's own timezone, falls in the calendar period.

    The frontend places each event on its local day, so the period is matched on local dates, not on UTC
    bounds. The database query takes the UTC period padded by ``CALENDAR_EDGE_PADDING`` (an indexed range);
    the local-date check then drops the events of the neighbouring periods. The queryset order is kept.

    Args:
        queryset: The events to filter, already filtered and ordered by the caller.
        week: ISO week number, as for ``calculate_calendar_date_range``.
        month: Month number, as for ``calculate_calendar_date_range``.
        year: Year, as for ``calculate_calendar_date_range``.

    Returns:
        The matching events, in queryset order.
    """
    start, end = calculate_calendar_date_range(week=week, month=month, year=year)
    first_day, end_day = start.date(), end.date()
    # select_related is a no-op when the caller already joins city; it keeps get_event_timezone free of N+1.
    candidates = queryset.filter(
        start__gte=start - CALENDAR_EDGE_PADDING, start__lt=end + CALENDAR_EDGE_PADDING
    ).select_related("city")
    # ponytail: the local-date check runs in Python over the padded result (at most one year of events).
    # Upgrade path if a year view grows too large: compare the start date at the city timezone in SQL.
    return [
        event for event in candidates if first_day <= event.start.astimezone(get_event_timezone(event)).date() < end_day
    ]


def order_by_distance(point: Point | None, queryset: QuerySet[T], *tie_break: str) -> QuerySet[T]:
    """Order a queryset by distance from a point, then by the tie-break fields.

    Rows at the same distance (e.g. venues in one city) are ordered by ``tie_break``. End it with a unique
    field, so that pagination is stable.

    Args:
        point: The reference point to measure distance from. ``None`` orders by ``tie_break`` only.
        queryset: A queryset of models with a location field.
        *tie_break: The fields that order rows at the same distance (e.g. ``"start", "id"``).

    Returns:
        The ordered queryset.

    Raises:
        ValueError: If ``tie_break`` is empty.
    """
    if not tie_break:
        raise ValueError("order_by_distance needs at least one tie-break field.")
    if point is None:
        return queryset.order_by(*tie_break)

    return queryset.annotate(  # type: ignore[return-value]
        distance=Distance("location", point),
    ).order_by("distance", *tie_break)
