"""A series hidden while a checkout waits for the pass lock is refused (#1566).

The purchase reads the series visibility after it gets the pass row lock. Under READ COMMITTED that read sees
what committed during the wait, so a series that the organizer hides while another transaction holds the
pass lock answers 404. A read before the locks would miss it.

The holder locks only the pass row, so the purchase (account lock, then pass lock) waits on the pass row.

``transaction=True``: each thread uses its own connection and must see the other's real commits and lock
waits, which the rollback-wrapped default ``django_db`` transaction cannot provide.
"""

import typing as t

import pytest
from django.db import transaction
from ninja_extra.exceptions import NotFound

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, Organization, SeriesPass
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tests.test_cancel_race_1431_1450 import _assert_waits_on_lock, _Pause, _Runner


def _pass_lock(sql: str) -> bool:
    return "FOR UPDATE" in sql and sql.partition(" FROM ")[2].split()[:1] == ['"events_seriespass"']


def _hold_the_pass_lock(series_pass_pk: t.Any) -> None:
    with transaction.atomic():
        # of=("self",) and no ordering: SeriesPass.Meta.ordering joins the series and the organization, and a plain
        # FOR UPDATE would lock them too, so the organization update below would wait for this holder.
        list(SeriesPass.objects.select_for_update(of=("self",)).filter(pk=series_pass_pk).order_by().values_list("pk"))


def _purchase(series_pass_pk: t.Any, user_pk: t.Any) -> t.Any:
    series_pass = SeriesPass.objects.select_related("event_series__organization__city").get(pk=series_pass_pk)
    user = RevelUser.objects.get(pk=user_pk)
    return SeriesPassPurchaseService(series_pass, user, check_visibility=True).purchase()


@pytest.mark.django_db(transaction=True)
def test_a_series_hidden_during_the_lock_wait_is_refused(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
) -> None:
    organization_pk = series_pass.event_series.organization_id
    Organization.objects.filter(pk=organization_pk).update(visibility=Organization.Visibility.PUBLIC)
    runner = _Runner()
    pause = _Pause(_pass_lock)
    runner.start("holder", lambda: _hold_the_pass_lock(series_pass.pk), pause)
    try:
        assert pause.reached.wait(timeout=10), "the holder never locked the pass"
        purchase = runner.start("purchase", lambda: _purchase(series_pass.pk, revel_user.pk))
        _assert_waits_on_lock(purchase)
        Organization.objects.filter(pk=organization_pk).update(visibility=Organization.Visibility.PRIVATE)
    finally:
        pause.release.set()
        runner.join()

    status, error = runner.outcome["purchase"]
    assert status == "err" and isinstance(error, NotFound), runner.outcome["purchase"]
    assert not HeldSeriesPass.objects.filter(series_pass=series_pass).exists()
