"""A series moved to another organization while a checkout waits for the pass lock is refused (#1577).

The locking statement joins the series, but after a lock wait Postgres reads again only the locked pass row: the
joined series row keeps the statement's snapshot. So the purchase reads the organization in a new statement.

``transaction=True``: each thread uses its own connection and must see the other's real commits and lock
waits, which the rollback-wrapped default ``django_db`` transaction cannot provide.
"""

import typing as t

import pytest

from accounts.models import RevelUser
from events.exceptions import SeriesPassNotPurchasableError
from events.models import Event, EventSeries, HeldSeriesPass, Organization, SeriesPass
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tests.test_cancel_race_1431_1450 import _assert_waits_on_lock, _Pause, _Runner
from events.tests.test_series_pass.test_visibility_lock_wait_1566 import _hold_the_pass_lock, _pass_lock


def _purchase(series_pass_pk: t.Any, user_pk: t.Any) -> t.Any:
    series_pass = SeriesPass.objects.select_related("event_series__organization__city").get(pk=series_pass_pk)
    user = RevelUser.objects.get(pk=user_pk)
    return SeriesPassPurchaseService(series_pass, user).purchase()


@pytest.mark.django_db(transaction=True)
def test_a_series_moved_during_the_lock_wait_is_refused(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    other_org = Organization.objects.create(name="Other", slug="other-1577", owner=organization_owner_user)
    runner = _Runner()
    pause = _Pause(_pass_lock)
    runner.start("holder", lambda: _hold_the_pass_lock(series_pass.pk), pause)
    try:
        assert pause.reached.wait(timeout=10), "the holder never locked the pass"
        purchase = runner.start("purchase", lambda: _purchase(series_pass.pk, revel_user.pk))
        _assert_waits_on_lock(purchase)
        EventSeries.objects.filter(pk=series_pass.event_series_id).update(organization=other_org)
    finally:
        pause.release.set()
        runner.join()

    status, error = runner.outcome["purchase"]
    assert status == "err" and isinstance(error, SeriesPassNotPurchasableError), runner.outcome["purchase"]
    assert str(error) == "The events of this pass changed. Please try again."
    assert not HeldSeriesPass.objects.filter(series_pass=series_pass).exists()
