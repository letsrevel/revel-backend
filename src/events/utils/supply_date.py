"""Date / period of supply for attendee invoices (EN 16931 BT-72 / BG-14)."""

import datetime
import typing as t

from events.utils import get_event_timezone

if t.TYPE_CHECKING:
    from events.models import Event


def supply_dates_for(event: "Event") -> tuple[datetime.date, datetime.date | None]:
    """Return the supply date and, for multi-day events, the period end.

    Dates are taken in the event's timezone (city timezone, else UTC). Open-ended events
    have a synthetic start + 24h ``end``, so they never claim a period.

    Args:
        event: The event the admission is supplied for.

    Returns:
        ``(start_date, end_date)``; ``end_date`` is ``None`` when the event ends on its start date.
    """
    tz = get_event_timezone(event)
    start = event.start.astimezone(tz).date()
    if event.is_open_ended:
        return start, None
    end = event.end.astimezone(tz).date()
    return start, (end if end != start else None)
