"""Regression guards for #880: dropping ``.distinct()`` must not introduce duplicates.

DISTINCT over the multi-join ``full()``/``with_event_details()`` selects costs hundreds of
milliseconds of Postgres *planning* time per request while deduplicating nothing (every
filter/search field on these endpoints is a to-one join). These tests pin both halves of
that claim: the endpoints never emit SELECT DISTINCT, **and** the responses contain no
duplicate rows even under the conditions that would fan out if any join were multi-valued —
M2M membership-tier restrictions on the tier, a search term matching several joined fields
at once, and multi-value status filters.
"""

import typing as t
from datetime import timedelta

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventRSVP, MembershipTier, Ticket

pytestmark = pytest.mark.django_db


def _get_checked(client: Client, url: str, params: dict[str, t.Any] | None = None) -> dict[str, t.Any]:
    """GET the url asserting 200, no SELECT DISTINCT, and no duplicate result ids."""
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url, params or {})
    assert response.status_code == 200
    offenders = [q["sql"] for q in ctx.captured_queries if "SELECT DISTINCT" in q["sql"]]
    assert not offenders, f"SELECT DISTINCT reintroduced (#880): {offenders[0][:200]}"
    data: dict[str, t.Any] = response.json()
    ids = [item["id"] for item in data["results"]]
    assert len(ids) == len(set(ids)), f"duplicate rows in response: {ids}"
    return data


@pytest.fixture
def fanout_event(dashboard_setup: dict[str, t.Any]) -> Event:
    """An event whose name/description and tier name all match one search term,

    and whose tier is restricted to TWO membership tiers — the M2M that would
    duplicate rows if it were ever joined instead of prefetched.
    """
    org = dashboard_setup["orgs"]["ticket"]
    event = Event.objects.create(
        organization=org,
        name="Gala Night",
        slug="gala-night",
        description="Gala Night is the event of the season",
        status="open",
        start=timezone.now() + timedelta(days=3),
        end=timezone.now() + timedelta(days=4),
    )
    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.name = "Gala Night Pass"
    tier.save(update_fields=["name"])
    mt1 = MembershipTier.objects.create(organization=org, name="Gold")
    mt2 = MembershipTier.objects.create(organization=org, name="Silver")
    tier.restricted_to_membership_tiers.add(mt1, mt2)
    return event


def test_dashboard_tickets_no_duplicates_with_m2m_and_search(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    fanout_event: Event,
) -> None:
    """Two tickets on an M2M-restricted tier come back exactly once each, searched or not."""
    tier = fanout_event.ticket_tiers.first()
    assert tier is not None
    t1 = Ticket.objects.create(guest_name="G1", event=fanout_event, user=dashboard_user, tier=tier)
    t2 = Ticket.objects.create(
        guest_name="G2", event=fanout_event, user=dashboard_user, tier=tier, status=Ticket.TicketStatus.PENDING
    )

    url = reverse("api:dashboard_tickets")

    data = _get_checked(dashboard_client, url)
    ids = {item["id"] for item in data["results"]}
    assert {str(t1.id), str(t2.id)} <= ids

    # Search term matches event name, event description AND tier name simultaneously —
    # the OR across three joined fields is the classic would-be fan-out shape.
    data = _get_checked(dashboard_client, url, {"search": "Gala Night"})
    assert data["count"] == 2
    assert {item["id"] for item in data["results"]} == {str(t1.id), str(t2.id)}

    # Filters + search combined still exact.
    data = _get_checked(dashboard_client, url, {"search": "Gala Night", "status": "pending"})
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(t2.id)


def test_dashboard_tickets_no_distinct_baseline(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    """The seeded ticket serves a non-empty page with no DISTINCT and no duplicates."""
    data = _get_checked(dashboard_client, reverse("api:dashboard_tickets"))
    assert data["count"] >= 1  # non-empty: the wide query actually ran


def test_dashboard_rsvps_no_duplicates_multi_status(
    dashboard_client: Client,
    dashboard_user: RevelUser,
    fanout_event: Event,
    dashboard_setup: dict[str, t.Any],
) -> None:
    """Multi-value status filter plus search returns each RSVP exactly once."""
    EventRSVP.objects.create(event=fanout_event, user=dashboard_user, status="yes")

    url = reverse("api:dashboard_rsvps")
    data = _get_checked(dashboard_client, url)
    assert data["count"] == 2  # setup's RSVP + this one

    # status is a list filter (status__in) — the multi-VALUE (not multi-join) case.
    data = _get_checked(dashboard_client, url, {"status": ["yes", "maybe"], "search": "Gala Night"})
    assert data["count"] == 1


def test_dashboard_invitation_requests_no_distinct(dashboard_client: Client, dashboard_setup: dict[str, t.Any]) -> None:
    _get_checked(dashboard_client, reverse("api:dashboard_invitation_requests"))
