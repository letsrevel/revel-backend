"""A series or organization delete and an event duplicate in it do not deadlock (#1538).

The duplicate locks its template event (``FOR NO KEY UPDATE``) and inserts a copy that references the series
and the organization. At commit, the copy's foreign-key checks take ``KEY SHARE`` on those rows. The delete
locks the series and the organization with ``FOR NO KEY UPDATE``, which does not conflict with ``KEY SHARE``,
and then waits for the template event. So the duplicate commits, and the delete then removes the copy too.
With ``FOR UPDATE`` on the series or the organization, the two waited for each other.

``transaction=True``: each thread uses its own connection and must see the other's real commits and lock
waits, which the rollback-wrapped default ``django_db`` transaction cannot provide.
"""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.contrib.admin.sites import AdminSite
from django.db import transaction

from events.admin.event import EventSeriesAdmin
from events.admin.organization import OrganizationAdmin
from events.models import Event, EventSeries, Organization
from events.service import event_series_service
from events.service.duplication import duplicate_event
from events.tests.test_cancel_race_1431_1450 import _assert_waits_on_lock, _Pause, _Runner


def _lock_of(table: str, mode: str) -> t.Callable[[str], bool]:
    """A lock statement on ``table`` in ``mode`` ("FOR UPDATE" does not match "FOR NO KEY UPDATE")."""

    def predicate(sql: str) -> bool:
        _, sep, rest = sql.partition(" FROM ")
        return bool(sep) and mode in sql and rest.split()[:1] == [table]

    return predicate


def _duplicate(template_pk: t.Any) -> Event:
    with transaction.atomic():
        template = Event.objects.get(pk=template_pk)
        return duplicate_event(template, "Copy", template.start + timedelta(days=7))


def _delete_series(series_pk: t.Any) -> None:
    event_series_service.delete_event_series(EventSeries.objects.get(pk=series_pk))


def _delete_organization(organization_pk: t.Any) -> None:
    with transaction.atomic():
        OrganizationAdmin(Organization, AdminSite()).delete_model(
            MagicMock(), Organization.objects.get(pk=organization_pk)
        )


def _bulk_delete_series(series_pk: t.Any) -> None:
    with transaction.atomic():
        EventSeriesAdmin(EventSeries, AdminSite()).delete_queryset(
            MagicMock(), EventSeries.objects.filter(pk=series_pk)
        )


DELETES: dict[str, t.Callable[[Event, EventSeries, Organization], None]] = {
    "series": lambda e, s, o: _delete_series(s.pk),
    "organization": lambda e, s, o: _delete_organization(o.pk),
    "series bulk": lambda e, s, o: _bulk_delete_series(s.pk),
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("target", list(DELETES))
def test_a_delete_waits_for_a_duplicate_in_flight_and_then_removes_the_copy(
    event: Event, event_series: EventSeries, organization: Organization, target: str
) -> None:
    """The duplicate holds its template event when the delete starts: the delete waits, then removes the copy."""
    runner = _Runner()
    pause = _Pause(_lock_of('"events_event"', "FOR NO KEY UPDATE"))
    runner.start("duplicate", lambda: _duplicate(event.pk), pause)
    try:
        assert pause.reached.wait(timeout=10), "the duplicate never reached its template lock"
        deletion = runner.start("delete", lambda: DELETES[target](event, event_series, organization))
        _assert_waits_on_lock(deletion)
    finally:
        pause.release.set()
        runner.join()

    assert runner.outcome["duplicate"][0] == "ok", runner.outcome["duplicate"]
    assert runner.outcome["delete"][0] == "ok", runner.outcome["delete"]
    assert not Event.objects.filter(organization_id=organization.pk).exists()
    assert not EventSeries.objects.filter(pk=event_series.pk).exists()
