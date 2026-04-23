"""Tests for ``recurrence_service.detect_cadence_drift``.

These live at the service layer because drift detection has enough branching
(rule-free series, no-future-events, exdates, modified/cancelled exclusions)
to warrant isolated unit coverage in addition to the controller-level tests.

Uses ``freeze_time`` so past-dated events and rule cadence changes can be
described with literal dates without fighting the wall clock.
"""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, RecurrenceRule
from events.service import recurrence_service

pytestmark = pytest.mark.django_db


def _make_occurrence(
    series: EventSeries,
    start: datetime,
    *,
    status: str = Event.EventStatus.DRAFT,
    is_modified: bool = False,
) -> Event:
    """Create a materialized (non-template) occurrence on ``series`` at ``start``.

    Bypasses ``materialize_occurrence`` / ``duplicate_event`` so tests can plant
    occurrences at specific instants (on- or off-cadence) without re-running the
    generation pipeline.
    """
    return Event.objects.create(
        organization=series.organization,
        event_series=series,
        name="Occurrence",
        start=start,
        end=start + timedelta(hours=2),
        status=status,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=False,
        is_modified=is_modified,
    )


class TestDetectCadenceDrift:
    """Happy-path and exclusion rules for ``detect_cadence_drift``."""

    def test_returns_empty_when_no_recurrence_rule(
        self,
        event_series: EventSeries,
    ) -> None:
        """A series with no rule cannot drift — short-circuit path."""
        assert event_series.recurrence_rule is None
        assert recurrence_service.detect_cadence_drift(event_series) == []

    def test_returns_empty_when_no_future_events(
        self,
        active_series: EventSeries,
    ) -> None:
        """No qualifying events means nothing to compare — avoids calling
        ``rule.between`` with a bogus range.
        """
        # active_series' dtstart is 2026-04-06; freeze well after so no events
        # would qualify even if they existed.
        with freeze_time("2026-05-01 00:00:00"):
            assert recurrence_service.detect_cadence_drift(active_series) == []

    @freeze_time("2026-04-06 00:00:00")
    def test_returns_empty_when_all_events_match_rule(
        self,
        active_series: EventSeries,
    ) -> None:
        """Events sitting exactly on the weekly-Monday rule must not drift."""
        # active_series rule: weekly Mondays at 2026-04-06 10:00.
        dt1 = timezone.make_aware(datetime(2026, 4, 6, 10, 0))
        dt2 = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        _make_occurrence(active_series, dt1)
        _make_occurrence(active_series, dt2)

        assert recurrence_service.detect_cadence_drift(active_series) == []

    @freeze_time("2026-04-06 00:00:00")
    def test_returns_stale_ids_when_events_drift_from_rule(
        self,
        active_series: EventSeries,
    ) -> None:
        """Events that aren't produced by the current rule are reported as stale.

        Simulates the cadence-change scenario from the issue: old occurrences
        scheduled under "weekly Mondays" are still in the DB; the operator has
        since changed the rule to "weekly Tuesdays". The Monday events no
        longer match and should be flagged.
        """
        # Old-cadence occurrences on Mondays.
        monday = timezone.make_aware(datetime(2026, 4, 6, 10, 0))
        next_monday = monday + timedelta(days=7)
        stale_a = _make_occurrence(active_series, monday)
        stale_b = _make_occurrence(active_series, next_monday)

        # Mutate the rule to "weekly Tuesdays" to represent the operator's PATCH.
        assert active_series.recurrence_rule is not None
        active_series.recurrence_rule.weekdays = [1]  # Tuesday
        active_series.recurrence_rule.dtstart = monday + timedelta(days=1)
        active_series.recurrence_rule.save()

        result = recurrence_service.detect_cadence_drift(active_series)
        assert set(result) == {stale_a.id, stale_b.id}

    @freeze_time("2026-04-06 00:00:00")
    def test_excludes_modified_occurrences(
        self,
        active_series: EventSeries,
    ) -> None:
        """``is_modified=True`` events are deliberate overrides — never flagged.

        The admin workflow for drift is "which occurrences are off-cadence
        because the rule changed?". Manually-shifted occurrences were moved
        intentionally and would hijack a "cancel all stale dates" bulk action.
        """
        # Off-cadence Tuesday event, but marked as manually modified.
        tuesday = timezone.make_aware(datetime(2026, 4, 7, 10, 0))
        _make_occurrence(active_series, tuesday, is_modified=True)

        assert recurrence_service.detect_cadence_drift(active_series) == []

    @freeze_time("2026-04-06 00:00:00")
    def test_excludes_cancelled_occurrences(
        self,
        active_series: EventSeries,
    ) -> None:
        """Already-cancelled events are terminal — no point flagging for re-cancel."""
        tuesday = timezone.make_aware(datetime(2026, 4, 7, 10, 0))
        _make_occurrence(active_series, tuesday, status=Event.EventStatus.CANCELLED)

        assert recurrence_service.detect_cadence_drift(active_series) == []

    @freeze_time("2026-04-08 00:00:00")
    def test_excludes_past_events(
        self,
        active_series: EventSeries,
    ) -> None:
        """Past occurrences can't be rescheduled, so they aren't part of the
        cadence-change workflow.
        """
        # A past (2026-04-07) off-cadence event.
        past_tuesday = timezone.make_aware(datetime(2026, 4, 7, 10, 0))
        _make_occurrence(active_series, past_tuesday)

        assert recurrence_service.detect_cadence_drift(active_series) == []

    @freeze_time("2026-04-06 00:00:00")
    def test_excludes_template_event(
        self,
        active_series: EventSeries,
    ) -> None:
        """The template event is never a real occurrence; it must never show up
        in the drift list even when its start sits off the current rule.
        """
        # Change the rule so the template's start (a Monday) doesn't match.
        assert active_series.recurrence_rule is not None
        active_series.recurrence_rule.weekdays = [1]  # Tuesday
        active_series.recurrence_rule.dtstart = timezone.make_aware(datetime(2026, 4, 7, 10, 0))
        active_series.recurrence_rule.save()

        # No non-template occurrences → empty.
        assert recurrence_service.detect_cadence_drift(active_series) == []

    @freeze_time("2026-04-06 00:00:00")
    def test_event_on_exdate_is_reported_as_drift(
        self,
        active_series: EventSeries,
    ) -> None:
        """Exdates are removed from the expected set, so any event still sitting
        on an exdate's instant is classified as drift.

        This is mostly a defensive shape check — normally an exdate implies the
        event is also cancelled — but it documents that ``_parse_exdates`` is
        actually applied to the expected set.
        """
        monday = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        event = _make_occurrence(active_series, monday)
        # Cancelling adds to exdates AND flips the event to CANCELLED; we want
        # an exdate without the cancellation to isolate the exdate-only path.
        active_series.exdates = [monday.isoformat()]
        active_series.save(update_fields=["exdates"])

        result = recurrence_service.detect_cadence_drift(active_series)
        assert event.id in result

    @freeze_time("2026-04-06 00:00:00")
    def test_rrule_computed_with_utc_normalization(
        self,
        active_series: EventSeries,
    ) -> None:
        """Event starts and ``to_rrule()`` outputs must compare as the same
        instant even when their tzinfo representations differ.

        Django (``USE_TZ=True``) stores event ``start`` fields as aware
        datetimes in UTC; ``dateutil.rrule`` returns aware datetimes carrying
        whatever tzinfo ``dtstart`` had (e.g. ``Europe/Vienna``). Those are
        the same instant but fail a direct ``==`` because their tzinfo objects
        differ. This regression test pins the UTC-normalization step: drop
        ``.astimezone(UTC)`` and on-cadence events start spuriously drifting.
        """
        # On-cadence Monday — must not drift.
        on_rule = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        _make_occurrence(active_series, on_rule)

        assert recurrence_service.detect_cadence_drift(active_series) == []


class TestDetectCadenceDriftWithoutFixtures:
    """Drift cases that don't need the shared ``active_series`` fixture."""

    def test_rule_with_count_exhausted_reports_all_future_as_stale(self) -> None:
        """If the operator sets ``count`` so tightly that no future occurrences
        are produced, every qualifying future event is by definition off-cadence.
        This exercises the ``rule.between`` returning an empty list branch.
        """
        owner = RevelUser.objects.create_user(username="drift_owner", email="drift@example.com", password="p")
        org = Organization.objects.create(name="DriftOrg", slug="drift-org", owner=owner)

        # Daily rule with count=1 and dtstart in the past — no future occurrences
        # produced by the rule, so any future event is drift.
        past = timezone.now() - timedelta(days=30)
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceRule.Frequency.DAILY,
            interval=1,
            dtstart=past,
            count=1,
        )
        series = EventSeries.objects.create(
            organization=org,
            name="Drifty",
            recurrence_rule=rule,
        )
        template = Event.objects.create(
            organization=org,
            event_series=series,
            name="Template",
            start=past,
            end=past + timedelta(hours=1),
            status=Event.EventStatus.DRAFT,
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            is_template=True,
        )
        series.template_event = template
        series.save(update_fields=["template_event"])

        future = timezone.now() + timedelta(days=1)
        stale = _make_occurrence(series, future)

        result = recurrence_service.detect_cadence_drift(series)
        assert result == [stale.id]
