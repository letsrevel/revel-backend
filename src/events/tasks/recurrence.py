"""Celery tasks for generating events from recurring series."""

import typing as t

import structlog
from celery import shared_task
from django.db import DatabaseError
from django.db import OperationalError as DjangoOperationalError
from kombu.exceptions import OperationalError as KombuOperationalError

logger = structlog.get_logger(__name__)


class RecurringEventGenerationResult(t.TypedDict):
    """Telemetry counters returned by ``generate_recurring_events_task``."""

    series_dispatched: int


@shared_task(name="events.generate_recurring_events")
def generate_recurring_events_task() -> RecurringEventGenerationResult:
    """Maintain the rolling generation window for all active recurring series.

    Runs daily via Celery Beat (scheduled by migration 0067). Dispatches a
    per-series subtask for each active series so that a failure on one series
    does not block the others, and each generation runs in its own atomic
    transaction. Idempotent — safe to re-run (skips already-existing
    occurrences).

    The per-series dispatch is wrapped in a narrow try/except for transport
    failures only (broker disconnect, queue declaration errors). Programming
    errors in the subtask itself propagate through the broker and are
    surfaced via the subtask's own retry/failure semantics — they should
    never be silently swallowed by the dispatcher.
    """
    from events.models import EventSeries

    series_ids = list(
        EventSeries.objects.filter(
            recurrence_rule__isnull=False,
            template_event__isnull=False,
            is_active=True,
        ).values_list("id", flat=True)
    )

    dispatched = 0
    for series_id in series_ids:
        try:
            generate_single_series_events_task.delay(str(series_id))
        except KombuOperationalError:
            # Broker is unreachable or refused the publish — log and continue
            # so one bad publish doesn't strand the rest of the batch. The
            # next daily run will retry. Anything other than a transport
            # failure should propagate.
            logger.exception("recurring_events_dispatch_failed", series_id=str(series_id))
            continue
        dispatched += 1

    # Report *successful* publishes, not just attempted ones, so a partial
    # broker outage doesn't look like full success in metrics.
    logger.info(
        "recurring_events_generation_dispatched",
        series_attempted=len(series_ids),
        series_dispatched=dispatched,
    )
    return {"series_dispatched": dispatched}


@shared_task(
    name="events.generate_single_series_events",
    autoretry_for=(DjangoOperationalError, DatabaseError),
    retry_backoff=60,
    retry_backoff_max=3600,
    max_retries=3,
)
def generate_single_series_events_task(series_id: str) -> int:
    """Generate rolling-window events for a single recurring series.

    Dispatched by :func:`generate_recurring_events_task`. Retries with
    exponential backoff only on transient database failures (deadlocks,
    connection drops). Programming errors (missing template, invalid rule,
    etc.) fail loudly on the first run instead of burning ~70 minutes of
    backoff before surfacing.

    Returns:
        The number of events created for this series.
    """
    from events.models import EventSeries
    from events.service.recurrence_service import generate_series_events

    series = (
        EventSeries.objects.select_related(
            "recurrence_rule",
            "template_event",
            "organization",
            "organization__owner",
        )
        .prefetch_related("organization__staff_members")
        .get(pk=series_id)
    )
    created = generate_series_events(series)
    logger.info(
        "recurring_events_series_generated",
        series_id=series_id,
        events_created=len(created),
    )
    return len(created)
