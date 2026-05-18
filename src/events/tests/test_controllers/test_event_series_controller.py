"""Public event-series controller tests.

Covers list/retrieve concerns that aren't tied to admin or follow flows. The
core invariant pinned here is that ``is_recurring`` is exposed without
introducing an N+1 against ``RecurrenceRule``.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from events.models import EventSeries, Organization, RecurrenceRule

pytestmark = pytest.mark.django_db


def _make_recurring_series(organization: Organization, name: str, slug: str) -> EventSeries:
    """Create an EventSeries wired to a RecurrenceRule (no template event needed for this contract)."""
    dtstart = timezone.now() + timedelta(days=1)
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],
        dtstart=dtstart,
    )
    return EventSeries.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        recurrence_rule=rule,
    )


def test_list_event_series_exposes_is_recurring(
    organization_owner_client: Client,
    organization: Organization,
    event_series: EventSeries,
) -> None:
    """The list endpoint surfaces ``is_recurring`` for both grouping and recurring series."""
    recurring_one = _make_recurring_series(organization, name="Recurring One", slug="recurring-one")
    recurring_two = _make_recurring_series(organization, name="Recurring Two", slug="recurring-two")

    response = organization_owner_client.get(reverse("api:list_event_series"))

    assert response.status_code == 200
    items = response.json()["results"]
    assert len(items) == 3
    by_id = {item["id"]: item["is_recurring"] for item in items}
    assert by_id[str(event_series.id)] is False
    assert by_id[str(recurring_one.id)] is True
    assert by_id[str(recurring_two.id)] is True


def test_with_is_recurring_annotation_has_no_n_plus_one(
    organization: Organization,
    event_series: EventSeries,
) -> None:
    """``with_is_recurring`` query count must not scale with the number of rows.

    The annotation is an ``EXISTS`` subquery, so reading ``is_recurring`` on every row of the
    result set must not trigger any follow-up queries. We compare the query count between
    N=1 and N=3 rows: if it stays constant, there is no N+1.
    """

    def _fetch() -> list[tuple[object, bool]]:
        qs = EventSeries.objects.with_is_recurring().order_by("name")
        return [(s.pk, getattr(s, "is_recurring")) for s in qs]

    with CaptureQueriesContext(connection) as ctx_one:
        baseline = _fetch()

    _make_recurring_series(organization, name="Recurring One", slug="recurring-one")
    _make_recurring_series(organization, name="Recurring Two", slug="recurring-two")

    with CaptureQueriesContext(connection) as ctx_three:
        results = _fetch()

    assert len(baseline) == 1
    assert len(results) == 3
    # 1 grouping (event_series fixture) + 2 recurring
    assert sum(1 for _, is_recurring in results if is_recurring) == 2
    assert sum(1 for _, is_recurring in results if not is_recurring) == 1
    # The actual N+1 canary: query count must not depend on row count.
    assert len(ctx_three.captured_queries) == len(ctx_one.captured_queries)


def test_retrieve_event_series_exposes_is_recurring(
    organization_owner_client: Client,
    organization: Organization,
) -> None:
    """The retrieve endpoint also surfaces ``is_recurring`` (recurring case)."""
    series = _make_recurring_series(organization, name="Detail Recurring", slug="detail-recurring")

    url = reverse("api:get_event_series", kwargs={"series_id": series.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    assert response.json()["is_recurring"] is True


def test_retrieve_grouping_series_reports_not_recurring(
    organization_owner_client: Client,
    event_series: EventSeries,
) -> None:
    """A grouping-only series (no RecurrenceRule attached) reports ``is_recurring=False``."""
    url = reverse("api:get_event_series", kwargs={"series_id": event_series.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    assert response.json()["is_recurring"] is False
