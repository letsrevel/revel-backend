"""The event admin change view locks the passes, the tiers and the event before it validates the submit (#1618).

The submit writes the event and then its tier inlines. A new series pass link locks the pass, then the tier, then the
event. Without the early lock, the two could deadlock.
"""

import typing as t
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import EventSeries, SeriesPass, TicketTier
from events.tests.test_series_pass.covered_events import cover

pytestmark = pytest.mark.django_db


@pytest.fixture
def series_pass(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Season",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def no_manifest_storage(settings: t.Any) -> None:
    """The change page renders unfold's static files, which have no manifest in tests."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def _first(sqls: list[str], table: str) -> int:
    return next(i for i, sql in enumerate(sqls) if "FOR NO KEY UPDATE" in sql and f'FROM "{table}"' in sql)


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_change_submit_locks_pass_then_tiers_then_event_first(
    client: Client, superuser: RevelUser, series_pass: SeriesPass
) -> None:
    event, _tier = cover(series_pass, "admin-lock", days=4)
    client.force_login(superuser)

    with CaptureQueriesContext(connection) as ctx:
        # An empty submit is invalid, so nothing is written: the locks come before the validation.
        response = client.post(reverse("admin:events_event_change", args=[event.pk]), data={})

    assert response.status_code == 200
    sqls = [q["sql"] for q in ctx.captured_queries]
    assert _first(sqls, "events_seriespass") < _first(sqls, "events_tickettier") < _first(sqls, "events_event")
    first_lock = _first(sqls, "events_seriespass")
    assert not any(sql.startswith(("UPDATE", "INSERT")) for sql in sqls[:first_lock])


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_get_takes_no_lock(client: Client, superuser: RevelUser, series_pass: SeriesPass) -> None:
    event, _tier = cover(series_pass, "admin-get", days=4)
    client.force_login(superuser)

    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("admin:events_event_change", args=[event.pk]))

    assert not any("FOR NO KEY UPDATE" in q["sql"] and "events_tickettier" in q["sql"] for q in ctx.captured_queries)
