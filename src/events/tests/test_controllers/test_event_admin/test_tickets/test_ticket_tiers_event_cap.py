"""Admin tier listing clamps ``total_available`` to the event's remaining capacity (#1209)."""

import typing as t

import orjson
import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from conftest import RevelUserFactory
from events.models import Event, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def _rows(client: Client, event: Event) -> dict[str, dict[str, t.Any]]:
    response = client.get(reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk}))
    assert response.status_code == 200, response.content
    # Events come with a default "General Admission" tier; ignore it.
    return {r["name"]: r for r in response.json()["results"] if r["name"] != "General Admission"}


@pytest.fixture
def capped_event(event: Event) -> Event:
    """The event with a 30-attendee cap."""
    event.max_attendees = 30
    event.save(update_fields=["max_attendees"])
    return event


def test_tier_larger_than_cap_is_clamped(
    organization_owner_client: Client, capped_event: Event, tier_factory: t.Callable[..., TicketTier]
) -> None:
    """A tier bigger than the cap reports the cap, flagged; its own quantity is untouched."""
    tier_factory(event=capped_event, name="Big", total_quantity=40)

    row = _rows(organization_owner_client, capped_event)["Big"]

    assert row["total_available"] == 30
    assert row["capped_by_event"] is True
    assert row["total_quantity"] == 40


def test_smaller_tier_keeps_its_own_figure(
    organization_owner_client: Client, capped_event: Event, tier_factory: t.Callable[..., TicketTier]
) -> None:
    """A tier under the cap is not flagged."""
    tier_factory(event=capped_event, name="Small", total_quantity=10)

    row = _rows(organization_owner_client, capped_event)["Small"]

    assert (row["total_available"], row["capped_by_event"]) == (10, False)


def test_unlimited_tier_reports_remaining_cap(
    organization_owner_client: Client, capped_event: Event, tier_factory: t.Callable[..., TicketTier]
) -> None:
    """An unlimited tier is bounded by the event cap."""
    tier_factory(event=capped_event, name="Open", total_quantity=None)

    row = _rows(organization_owner_client, capped_event)["Open"]

    assert (row["total_available"], row["capped_by_event"]) == (30, True)


def test_uncapped_event_returns_raw_values(
    organization_owner_client: Client, event: Event, tier_factory: t.Callable[..., TicketTier]
) -> None:
    """Without an event cap nothing is clamped or flagged."""
    event.max_attendees = 0
    event.save(update_fields=["max_attendees"])
    tier_factory(event=event, name="Big", total_quantity=40)
    tier_factory(event=event, name="Open", total_quantity=None)

    rows = _rows(organization_owner_client, event)

    assert (rows["Big"]["total_available"], rows["Big"]["capped_by_event"]) == (40, False)
    assert (rows["Open"]["total_available"], rows["Open"]["capped_by_event"]) == (None, False)


def test_sold_tickets_on_other_tier_reduce_it(
    organization_owner_client: Client,
    capped_event: Event,
    tier_factory: t.Callable[..., TicketTier],
    ticket_factory: t.Callable[..., Ticket],
    revel_user_factory: RevelUserFactory,
) -> None:
    """Tickets sold on another tier shrink what this one can still sell."""
    other = tier_factory(event=capped_event, name="Other", total_quantity=100)
    tier_factory(event=capped_event, name="Big", total_quantity=40)
    for _ in range(25):
        ticket_factory(event=capped_event, tier=other, user=revel_user_factory())

    row = _rows(organization_owner_client, capped_event)["Big"]

    assert (row["total_available"], row["capped_by_event"]) == (5, True)


def test_query_count_does_not_grow_with_tiers(
    organization_owner_client: Client, capped_event: Event, tier_factory: t.Callable[..., TicketTier]
) -> None:
    """The remaining capacity is computed once, not per tier."""
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": capped_event.pk})
    tier_factory(event=capped_event, name="T1", total_quantity=40)
    with CaptureQueriesContext(connection) as one_tier:
        organization_owner_client.get(url)
    tier_factory(event=capped_event, name="T2", total_quantity=40)
    tier_factory(event=capped_event, name="T3", total_quantity=None)
    with CaptureQueriesContext(connection) as three_tiers:
        organization_owner_client.get(url)

    assert len(three_tiers) == len(one_tier)


def test_create_response_returns_raw_figure(organization_owner_client: Client, capped_event: Event) -> None:
    """Create/update responses carry no annotation: raw figure, not flagged."""
    url = reverse("api:create_ticket_tier", kwargs={"event_id": capped_event.pk})
    payload = {"name": "Big", "price": "0", "total_quantity": 40, "payment_method": "offline"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    assert (response.json()["total_available"], response.json()["capped_by_event"]) == (40, False)
