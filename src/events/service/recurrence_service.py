"""Service for recurring event materialization and management."""

import typing as t
from datetime import datetime, timedelta

import structlog
from django.utils import timezone

from events.models import Event, EventSeries
from events.service.duplication import duplicate_event
from events.suppression import suppress_default_tier_creation, suppress_event_notifications

logger = structlog.get_logger(__name__)


def materialize_occurrence(series: EventSeries, dt: datetime, index: int) -> Event:
    """Generate a single Event from the series template.

    Calls duplicate_event() for deep cloning, then sets series-specific fields.
    The duplicate_event() call already uses suppress_default_tier_creation() internally.
    """
    template = series.template_event
    if not template:
        raise ValueError("Series has no template event.")

    event = duplicate_event(
        template_event=template,
        new_name=template.name,
        new_start=dt,
    )

    # Set series-specific fields. duplicate_event() already copies event_series from
    # the template, but we set it explicitly for clarity. Status depends on auto_publish.
    update_fields = ["occurrence_index"]
    event.occurrence_index = index

    if series.auto_publish:
        event.status = Event.EventStatus.OPEN
        update_fields.append("status")

    event.save(update_fields=update_fields)

    logger.info(
        "occurrence_materialized",
        series_id=str(series.id),
        event_id=str(event.id),
        occurrence_dt=dt.isoformat(),
        index=index,
        status=event.status,
    )
    return event


def generate_series_events(
    series: EventSeries,
    until_override: datetime | None = None,
) -> list[Event]:
    """Generate events for a recurring series within the rolling window.

    Skips excluded dates (exdates) and already-existing occurrences.
    Wraps batch in suppress_event_notifications() to prevent per-event spam.

    Args:
        series: The EventSeries with a recurrence_rule and template_event.
        until_override: Optional override for the generation horizon.

    Returns:
        List of newly created Event instances.
    """
    if not series.is_active:
        return []
    if not series.recurrence_rule or not series.template_event:
        return []

    rule = series.recurrence_rule.to_rrule()
    horizon = until_override or (timezone.now() + timedelta(weeks=series.generation_window_weeks))
    # Offset start_from by 1 second so dtstart itself is included in between()
    start_from = series.last_generated_until or (series.recurrence_rule.dtstart - timedelta(seconds=1))

    # Compute which dates to skip (using timezone-aware datetime comparison)
    exdates_set = _parse_exdates(series.exdates)
    existing_starts = set(series.events.filter(is_template=False).values_list("start", flat=True))

    occurrences = rule.between(start_from, horizon, inc=False)

    created: list[Event] = []
    with suppress_default_tier_creation(), suppress_event_notifications():
        for i, dt in enumerate(occurrences):
            if dt in exdates_set or dt in existing_starts:
                continue
            event = materialize_occurrence(series, dt, index=i)
            created.append(event)

    series.last_generated_until = horizon
    series.save(update_fields=["last_generated_until"])

    # Send one digest notification instead of N individual ones
    if created:
        from notifications.service.notification_helpers import notify_series_events_generated

        notify_series_events_generated(series, created)

    logger.info(
        "series_events_generated",
        series_id=str(series.id),
        count=len(created),
        horizon=horizon.isoformat(),
    )
    return created


def cancel_occurrence(series: EventSeries, occurrence_date: datetime) -> None:
    """Cancel a single occurrence by adding it to exdates.

    If the occurrence was already materialized, also cancels that event.
    """
    date_str = occurrence_date.isoformat()
    if date_str not in series.exdates:
        series.exdates = [*series.exdates, date_str]
        series.save(update_fields=["exdates"])

    # Cancel materialized event if it exists
    materialized = series.events.filter(
        is_template=False,
        start=occurrence_date,
    ).first()
    if materialized and materialized.status != Event.EventStatus.CANCELLED:
        materialized.status = Event.EventStatus.CANCELLED
        materialized.save(update_fields=["status"])
        logger.info(
            "occurrence_cancelled",
            series_id=str(series.id),
            event_id=str(materialized.id),
            occurrence_date=date_str,
        )


def pause_series(series: EventSeries) -> None:
    """Pause generation for a series without cancelling existing events."""
    series.is_active = False
    series.save(update_fields=["is_active"])


def resume_series(series: EventSeries) -> None:
    """Resume generation for a paused series."""
    series.is_active = True
    series.save(update_fields=["is_active"])


# Fields safe to propagate from template to occurrences. Excludes dates (start, end),
# status, slugs, FK references, and computed fields — these are per-occurrence state.
PROPAGATABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "invitation_message",
        "event_type",
        "visibility",
        "max_attendees",
        "max_tickets_per_user",
        "waitlist_open",
        "requires_ticket",
        "requires_full_profile",
        "potluck_open",
        "accept_invitation_requests",
        "public_pronoun_distribution",
        "can_attend_without_login",
        "address",
        "location",
        "address_visibility",
    }
)


def propagate_template_changes(
    series: EventSeries,
    changed_fields: dict[str, t.Any],
    scope: str,
) -> int:
    """Apply template field changes to future occurrences.

    Only fields in PROPAGATABLE_FIELDS are propagated. Date/time, status, slug,
    and FK fields are per-occurrence state and must not be overwritten.

    Args:
        series: The series whose template was updated.
        changed_fields: Dict of field_name -> new_value from the update payload.
        scope: "future_unmodified" or "all_future".

    Returns:
        Number of events updated.
    """
    qs = series.events.filter(
        is_template=False,
        start__gte=timezone.now(),
    )
    if scope == "future_unmodified":
        qs = qs.filter(is_modified=False)
    elif scope != "all_future":
        raise ValueError(f"Invalid propagation scope: {scope}")

    safe_changes = {k: v for k, v in changed_fields.items() if k in PROPAGATABLE_FIELDS}
    if not safe_changes:
        return 0

    with suppress_event_notifications():
        count = qs.update(**safe_changes)

    logger.info(
        "template_changes_propagated",
        series_id=str(series.id),
        scope=scope,
        fields=list(changed_fields.keys()),
        count=count,
    )
    return count


def _parse_exdates(exdates: list[str]) -> set[datetime]:
    """Parse ISO datetime strings from exdates into a set of timezone-aware datetimes."""
    from dateutil.parser import isoparse

    result: set[datetime] = set()
    for d in exdates:
        result.add(isoparse(d))
    return result
