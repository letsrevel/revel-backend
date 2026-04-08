"""Service for recurring event materialization and management."""

import enum
import typing as t
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import structlog
from dateutil.parser import isoparse
from django.db import models as db_models
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel

from events.models import Event, EventSeries, Organization, RecurrenceRule
from events.service.duplication import duplicate_event
from events.suppression import suppress_event_notifications

logger = structlog.get_logger(__name__)


class PropagateScope(enum.StrEnum):
    """Scope of template-change propagation to materialized occurrences."""

    NONE = "none"
    FUTURE_UNMODIFIED = "future_unmodified"
    ALL_FUTURE = "all_future"


# Fields safe to propagate from template to occurrences. Excludes dates (start, end),
# status, slugs, FK references, and computed fields — these are per-occurrence state.
# ``address`` and ``location`` are kept together as a coupled pair so we never leave an
# occurrence with a text address that disagrees with its PostGIS Point.
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

# ``address`` + ``location`` are stored on Event as a coupled pair (human-readable
# address text + PostGIS Point). Propagating one without the other would leave
# occurrences with mismatched coordinates, so we always write both together.
_COUPLED_FIELD_GROUPS: tuple[frozenset[str], ...] = (frozenset({"address", "location"}),)


def materialize_occurrence(series: EventSeries, dt: datetime, index: int) -> Event:
    """Generate a single Event from the series template.

    Calls duplicate_event() for deep cloning with series-specific overrides
    (occurrence_index and status) applied atomically in the initial create.
    The duplicate_event() call already uses suppress_default_tier_creation() internally.
    """
    template = series.template_event
    if not template:
        raise ValueError("Series has no template event.")

    status_override = Event.EventStatus.OPEN if series.auto_publish else None

    event = duplicate_event(
        template_event=template,
        new_name=template.name,
        new_start=dt,
        occurrence_index=index,
        status_override=status_override,
    )

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
        # An active series without a rule or template is a broken state:
        # CASCADE deletes normally prevent this, but a legacy half-null row
        # could still be in the DB. Log so operators notice and clean up.
        logger.warning(
            "series_missing_rule_or_template",
            series_id=str(series.id),
            has_rule=bool(series.recurrence_rule),
            has_template=bool(series.template_event),
        )
        return []

    rule = series.recurrence_rule.to_rrule()
    horizon = until_override or (timezone.now() + timedelta(weeks=series.generation_window_weeks))
    # Offset start_from by 1 second so dtstart itself is included in between().
    start_from = series.last_generated_until or (series.recurrence_rule.dtstart - timedelta(seconds=1))
    # Defense against horizon decreases (e.g. user lowers generation_window_weeks):
    # without this cap, start_from could exceed horizon and silently stall generation.
    if start_from > horizon:
        start_from = horizon

    # Compute which dates to skip (using timezone-aware datetime comparison).
    # Scope existing_starts to the window we're about to generate to avoid loading
    # the entire history of long-running series.
    exdates_set = _parse_exdates(series.exdates)
    existing_starts = set(
        series.events.filter(
            is_template=False,
            start__gte=start_from,
            start__lt=horizon,
        ).values_list("start", flat=True)
    )

    # Continue the occurrence_index sequence from where the last batch left off so
    # indices remain globally monotonic across rolling-window runs.
    max_index = series.events.filter(is_template=False).aggregate(max_idx=db_models.Max("occurrence_index"))["max_idx"]
    next_index = (max_index if max_index is not None else -1) + 1

    occurrences = rule.between(start_from, horizon, inc=False)

    created: list[Event] = []
    with suppress_event_notifications():
        for dt in occurrences:
            if dt in exdates_set or dt in existing_starts:
                continue
            event = materialize_occurrence(series, dt, index=next_index)
            created.append(event)
            next_index += 1

        series.last_generated_until = horizon
        series.save(update_fields=["last_generated_until"])

    # Send one digest notification instead of N individual ones.
    # Imported locally to avoid a circular import: notifications -> events -> notifications.
    if created:
        from notifications.service.notification_helpers import notify_series_events_generated  # noqa: PLC0415

        notify_series_events_generated(series, created)

    logger.info(
        "series_events_generated",
        series_id=str(series.id),
        count=len(created),
        horizon=horizon.isoformat(),
    )
    return created


@transaction.atomic
def create_recurring_event_series(
    organization: Organization,
    *,
    recurrence_data: dict[str, t.Any],
    series_name: str,
    series_description: str | None,
    auto_publish: bool,
    generation_window_weeks: int,
    event_data: dict[str, t.Any],
) -> EventSeries:
    """Create a recurring event series with template and initial generation.

    Creates: RecurrenceRule + EventSeries + template Event, then materializes
    events within the configured rolling window.
    """
    rule = RecurrenceRule.objects.create(**recurrence_data)

    series = EventSeries.objects.create(
        organization=organization,
        name=series_name,
        description=series_description,
        recurrence_rule=rule,
        auto_publish=auto_publish,
        generation_window_weeks=generation_window_weeks,
    )

    # Templates must stay in DRAFT (enforced by ``template_events_must_be_draft``
    # CheckConstraint). Whether an occurrence is auto-published at materialization
    # time is controlled by ``series.auto_publish``, not by the template's status.
    template_data = {**event_data, "status": Event.EventStatus.DRAFT}

    template_event = Event(
        organization=organization,
        event_series=series,
        is_template=True,
        **template_data,
    )
    template_event.save()  # TimeStampedModel.save() runs full_clean()

    series.template_event = template_event
    series.save(update_fields=["template_event"])

    generate_series_events(series)
    # refresh_from_db clears series.events cache so callers see the new occurrences.
    series.refresh_from_db()
    return series


def _normalize_exdate(value: str | datetime) -> str:
    """Normalize an exdate (string or datetime) to a UTC ISO 8601 string.

    Equivalent instants sent with different tz offsets round-trip to the same
    UTC-normalized representation, so membership comparisons via
    ``_normalize_exdate_set`` never accumulate duplicates.
    """
    dt = isoparse(value) if isinstance(value, str) else value
    return dt.astimezone(dt_timezone.utc).isoformat()


def _normalize_exdate_set(exdates: list[str]) -> set[str]:
    """Return a set of UTC-normalized exdate strings, skipping empty entries.

    Tolerates legacy entries that were stored with the sender's tz offset
    instead of UTC by round-tripping every entry through ``_normalize_exdate``.
    """
    return {_normalize_exdate(s) for s in exdates if s}


def cancel_occurrence(series: EventSeries, occurrence_date: datetime) -> None:
    """Cancel a single occurrence by adding it to exdates.

    The exdate is stored as a UTC-normalized ISO 8601 string so that equivalent
    instants sent in different timezone representations don't accumulate as
    duplicates. If the occurrence was already materialized, also cancels that event.
    """
    normalized = _normalize_exdate(occurrence_date)
    existing_normalized = _normalize_exdate_set(series.exdates)
    if normalized not in existing_normalized:
        series.exdates = [*series.exdates, normalized]
        series.save(update_fields=["exdates"])

    # Cancel materialized event if it exists. Match by instant, not wall-clock
    # representation, by using the original aware datetime (Django compares in UTC).
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
            occurrence_date=normalized,
        )


# Fields on the RecurrenceRule whose change should invalidate the rolling-window
# cursor (``EventSeries.last_generated_until``). A change to any of these means
# future occurrences are no longer predictable from the previous horizon, so we
# must regenerate from scratch on the next task run.
_RULE_CURSOR_INVALIDATING_FIELDS = frozenset(
    {
        "frequency",
        "interval",
        "weekdays",
        "monthly_type",
        "day_of_month",
        "nth_weekday",
        "weekday",
        "dtstart",
        "until",
        "count",
        "timezone",
    }
)


@transaction.atomic
def update_series_recurrence(
    series: EventSeries,
    *,
    auto_publish: bool | None = None,
    generation_window_weeks: int | None = None,
    recurrence_data: dict[str, t.Any] | None = None,
) -> EventSeries:
    """Update recurrence rule and/or series settings.

    Caller is expected to pass only fields the user actually sent (use
    ``model_dump(exclude_unset=True)``). ``recurrence_data`` may contain a
    partial update for the underlying ``RecurrenceRule``.

    When the rule or ``generation_window_weeks`` change in a way that would
    shift the occurrence schedule, ``last_generated_until`` is reset so that
    the next ``generate_series_events`` call produces the new cadence
    immediately rather than waiting for real time to catch up.
    """
    cursor_should_reset = False

    series_fields: list[str] = []
    if auto_publish is not None:
        series.auto_publish = auto_publish
        series_fields.append("auto_publish")
    if generation_window_weeks is not None and generation_window_weeks != series.generation_window_weeks:
        series.generation_window_weeks = generation_window_weeks
        series_fields.append("generation_window_weeks")
        # Any window change invalidates the cursor: decreases can stall the series,
        # increases should backfill into the new horizon immediately.
        cursor_should_reset = True

    if recurrence_data and series.recurrence_rule:
        rule_changed_fields = set(recurrence_data.keys()) & _RULE_CURSOR_INVALIDATING_FIELDS
        for field, value in recurrence_data.items():
            setattr(series.recurrence_rule, field, value)
        series.recurrence_rule.save()  # TimeStampedModel.save() runs full_clean()
        if rule_changed_fields:
            cursor_should_reset = True

    if cursor_should_reset:
        series.last_generated_until = None
        series_fields.append("last_generated_until")

    if series_fields:
        series.save(update_fields=series_fields)

    series.refresh_from_db()
    return series


def pause_series(series: EventSeries) -> None:
    """Pause generation for a series without cancelling existing events."""
    series.is_active = False
    series.save(update_fields=["is_active"])


def resume_series(series: EventSeries) -> None:
    """Resume generation for a paused series."""
    series.is_active = True
    series.save(update_fields=["is_active"])


@transaction.atomic
def update_template(
    series: EventSeries,
    payload: BaseModel,
    scope: PropagateScope,
) -> EventSeries:
    """Update a series' template event and optionally propagate changes to occurrences.

    Atomically:
    1. Update the template event from ``payload`` (``exclude_unset=True``).
    2. If ``scope`` is not ``NONE``, apply the same changes to future occurrences
       via ``propagate_template_changes``.

    Wrapping both steps in a single transaction prevents the half-applied state
    where the template is updated but propagation raises and the occurrences
    are left out of sync.

    Args:
        series: The series whose template should be updated. Must have a
            ``template_event`` set.
        payload: The ``TemplateEditSchema`` (or compatible) payload. Only fields
            the client explicitly sent are applied.
        scope: Propagation scope. ``PropagateScope.NONE`` updates the template
            only.

    Returns:
        The refreshed ``EventSeries``.

    Raises:
        ValueError: If the series has no template_event (caller should guard).
    """
    template = series.template_event
    if template is None:
        raise ValueError("Series has no template event.")

    # Local import to break import cycle: events.service.__init__ imports this
    # module transitively via recurrence_service re-exports.
    from events.service import update_db_instance  # noqa: PLC0415

    changed_data = payload.model_dump(exclude_unset=True)
    update_db_instance(template, payload)

    if scope != PropagateScope.NONE and changed_data:
        propagate_template_changes(series, changed_data, scope=scope)

    series.refresh_from_db()
    return series


def propagate_template_changes(
    series: EventSeries,
    changed_fields: dict[str, t.Any],
    scope: PropagateScope,
) -> int:
    """Apply template field changes to future occurrences.

    Only fields in PROPAGATABLE_FIELDS are propagated. Date/time, status, slug,
    and FK fields are per-occurrence state and must not be overwritten.

    Coupled field groups (e.g. ``address`` + ``location``) are propagated
    atomically: partial updates that would leave occurrences inconsistent are
    expanded to include the other coupled field(s) from the series template.

    Changes are written via per-instance ``save()`` so that ``full_clean()``
    runs and any coupled-field invariants enforced at the model layer are
    respected. Notifications are suppressed during the batch.

    Args:
        series: The series whose template was updated.
        changed_fields: Dict of field_name -> new_value from the update payload.
        scope: ``PropagateScope.FUTURE_UNMODIFIED`` or ``PropagateScope.ALL_FUTURE``.
            ``PropagateScope.NONE`` is a no-op.

    Returns:
        Number of events updated.
    """
    if scope == PropagateScope.NONE:
        return 0

    safe_changes = {k: v for k, v in changed_fields.items() if k in PROPAGATABLE_FIELDS}
    if not safe_changes:
        return 0

    # Expand coupled-field groups: if any member of a group is being propagated,
    # pull the other members from the template so occurrences stay consistent.
    template = series.template_event
    for group in _COUPLED_FIELD_GROUPS:
        touched = group & set(safe_changes.keys())
        if touched and touched != group and template is not None:
            for field in group - touched:
                safe_changes[field] = getattr(template, field)

    qs = series.events.filter(
        is_template=False,
        start__gte=timezone.now(),
    )
    if scope == PropagateScope.FUTURE_UNMODIFIED:
        qs = qs.filter(is_modified=False)

    update_fields = sorted(safe_changes.keys())
    count = 0
    with suppress_event_notifications():
        for event in qs:
            for field, value in safe_changes.items():
                setattr(event, field, value)
            event.save(update_fields=update_fields)
            count += 1

    logger.info(
        "template_changes_propagated",
        series_id=str(series.id),
        scope=str(scope),
        fields=update_fields,
        count=count,
    )
    return count


def _parse_exdates(exdates: list[str]) -> set[datetime]:
    """Parse exdate ISO strings into a set of UTC-normalized datetimes.

    Exdates may have been stored with the sender's tz offset (legacy) or as
    UTC (current format); both normalize to the same UTC instant here so that
    membership comparisons against rrule-generated datetimes match by instant.
    """
    return {isoparse(d).astimezone(dt_timezone.utc) for d in exdates if d}
