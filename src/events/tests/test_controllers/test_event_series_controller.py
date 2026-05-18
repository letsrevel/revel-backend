"""Public event-series controller tests.

Covers list/retrieve concerns that aren't tied to admin or follow flows. The
core invariant pinned here is that ``is_recurring`` is exposed without
introducing an N+1 against ``RecurrenceRule``.
"""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
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


def test_list_event_series_exposes_is_recurring_without_n_plus_one(
    organization_owner_client: Client,
    organization: Organization,
    event_series: EventSeries,
    django_assert_max_num_queries: t.Any,
) -> None:
    """The list endpoint must surface ``is_recurring`` and stay flat as the row count grows.

    We seed 1 grouping-only series (the ``event_series`` fixture) and 2 recurring series, then
    hit the list endpoint twice and assert the same query count: that's the N+1 canary. The
    bound is set generous-but-finite (auth + count + page + prefetches) so we don't paper over
    a regression by widening it later.
    """
    recurring_one = _make_recurring_series(organization, name="Recurring One", slug="recurring-one")
    recurring_two = _make_recurring_series(organization, name="Recurring Two", slug="recurring-two")

    url = reverse("api:list_event_series")

    # First call establishes the query budget; second call must match to prove no N+1.
    with django_assert_max_num_queries(20) as captured_first:
        response = organization_owner_client.get(url)
    assert response.status_code == 200

    payload = response.json()
    items = payload["items"]
    assert len(items) == 3

    by_id = {item["id"]: item["is_recurring"] for item in items}
    assert by_id[str(event_series.id)] is False
    assert by_id[str(recurring_one.id)] is True
    assert by_id[str(recurring_two.id)] is True

    # Second call: same query count regardless of the recurring-row count -> no N+1.
    first_count = len(captured_first.captured_queries)
    with django_assert_max_num_queries(first_count) as captured_second:
        response = organization_owner_client.get(url)
    assert response.status_code == 200
    assert len(captured_second.captured_queries) == first_count


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
