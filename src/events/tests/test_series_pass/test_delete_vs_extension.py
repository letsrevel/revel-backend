"""An event delete and a series pass extension on the same event serialize on the tier lock (#1538).

The extension locks the tier, then the event, and inserts the holder's ticket. The delete now locks the
tiers and the event before its cascade collects the tickets. So the delete either sees the new ticket and
removes it, or it commits first and the extension skips the deleted event. Before, the delete collected
the tickets first, then waited on the tier, and then failed (a deadlock or a foreign-key error).

``transaction=True``: each thread uses its own connection and must see the other's real commits and lock
waits, which the rollback-wrapped default ``django_db`` transaction cannot provide.
"""

import typing as t
from uuid import UUID

import pytest

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket
from events.service import event_update_service
from events.tasks.series_pass import materialize_series_pass_holders
from events.tests.test_cancel_race_1431_1450 import _assert_waits_on_lock, _Pause, _Runner
from events.tests.test_series_pass.covered_events import cover


def _lock_of(table: str) -> t.Callable[[str], bool]:
    def predicate(sql: str) -> bool:
        return "FOR UPDATE" in sql and sql.split(" FROM ")[1].split()[0] == f'"{table}"'

    return predicate


def _holder(series_pass: SeriesPass, user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=series_pass.price,
    )


def _delete(event_pk: UUID) -> None:
    event_update_service.delete_event(Event.objects.get(pk=event_pk))


@pytest.mark.django_db(transaction=True)
def test_a_delete_waits_for_an_extension_in_flight_and_then_removes_its_ticket(
    series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    """The extension holds the tier lock when the delete starts: the delete waits, then removes the new ticket."""
    event, _tier = cover(series_pass, "doomed", days=5)
    _holder(series_pass, revel_user)
    runner = _Runner()
    pause = _Pause(_lock_of("events_tickettier"))
    runner.start("extension", lambda: materialize_series_pass_holders(str(series_pass.pk), [str(event.pk)]), pause)
    assert pause.reached.wait(timeout=10), "the extension never reached its tier lock"
    deletion = runner.start("delete", lambda: _delete(event.pk))
    _assert_waits_on_lock(deletion)
    pause.release.set()
    runner.join()

    assert runner.outcome["extension"][0] == "ok", runner.outcome["extension"]
    assert runner.outcome["delete"][0] == "ok", runner.outcome["delete"]
    assert not Event.objects.filter(pk=event.pk).exists()
    assert not Ticket.objects.filter(event_id=event.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_an_extension_waits_for_a_delete_in_flight_and_then_skips_the_event(
    series_pass: SeriesPass, revel_user: RevelUser
) -> None:
    """The delete holds the event lock when the extension starts: the extension waits, then skips the event."""
    event, _tier = cover(series_pass, "doomed", days=5)
    kept, _kept_tier = cover(series_pass, "kept", days=6)
    holder = _holder(series_pass, revel_user)
    runner = _Runner()
    pause = _Pause(_lock_of("events_event"))
    runner.start("delete", lambda: _delete(event.pk), pause)
    assert pause.reached.wait(timeout=10), "the delete never reached its event lock"
    extension = runner.start(
        "extension", lambda: materialize_series_pass_holders(str(series_pass.pk), [str(event.pk), str(kept.pk)])
    )
    _assert_waits_on_lock(extension)
    pause.release.set()
    runner.join()

    assert runner.outcome["delete"][0] == "ok", runner.outcome["delete"]
    assert runner.outcome["extension"][0] == "ok", runner.outcome["extension"]
    assert not Event.objects.filter(pk=event.pk).exists()
    assert list(Ticket.objects.filter(held_pass=holder).values_list("event_id", flat=True)) == [kept.pk]
