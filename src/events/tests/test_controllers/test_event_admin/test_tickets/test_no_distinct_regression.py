"""Regression guards for #880 on the admin ticket list: no DISTINCT, no duplicates.

Same rationale as ``test_controllers/test_no_distinct_regression.py`` — DISTINCT over the
wide ``full()`` join is pure planner cost and deduplicates nothing. These tests pin that
under the would-be fan-out conditions: an M2M-restricted tier, a search term matching
several ``user__*`` fields at once, the ``source`` filter, and ordering by the
``effective_price_paid`` annotation.
"""

import typing as t

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, MembershipTier, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def _get_checked(client: Client, url: str, params: dict[str, t.Any] | None = None) -> dict[str, t.Any]:
    """GET the url asserting 200, no ticket/tier SELECT DISTINCT, and no duplicate ids.

    The event-visibility gate (``Event.objects.for_user``) runs in the same request and
    keeps a legitimately load-bearing DISTINCT (M2M staff/member joins), so the guard
    targets only the wide ticket/tier list queries this fix touched.
    """
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url, params or {})
    assert response.status_code == 200
    offenders = [
        q["sql"]
        for q in ctx.captured_queries
        if "SELECT DISTINCT" in q["sql"]
        and ('FROM "events_ticket"' in q["sql"] or 'FROM "events_tickettier"' in q["sql"])
    ]
    assert not offenders, f"SELECT DISTINCT reintroduced (#880): {offenders[0][:200]}"
    data: dict[str, t.Any] = response.json()
    ids = [item["id"] for item in data["results"]]
    assert len(ids) == len(set(ids)), f"duplicate rows in response: {ids}"
    return data


def test_admin_list_tickets_no_duplicates(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    pending_offline_ticket: Ticket,
    public_user: RevelUser,
) -> None:
    """Tickets on an M2M-restricted tier, searched by a multi-field term, appear exactly once."""
    # The M2M that would duplicate rows if it were joined instead of prefetched.
    mt1 = MembershipTier.objects.create(organization=event.organization, name="Gold")
    mt2 = MembershipTier.objects.create(organization=event.organization, name="Silver")
    offline_tier.restricted_to_membership_tiers.add(mt1, mt2)

    # A holder whose email, first AND last name all match one search term — the OR across
    # several user__* joined fields is the would-be fan-out shape of the search decorator.
    public_user.first_name = "Zaphod"
    public_user.last_name = "Zaphodson"
    public_user.email = "zaphod@example.com"
    public_user.save(update_fields=["first_name", "last_name", "email"])
    second_ticket = Ticket.objects.create(
        guest_name="Second",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )

    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    data = _get_checked(organization_owner_client, url)
    assert data["count"] == 2

    data = _get_checked(organization_owner_client, url, {"search": "zaphod"})
    assert data["count"] == 2
    assert {item["id"] for item in data["results"]} == {str(pending_offline_ticket.id), str(second_ticket.id)}

    # Filters + annotation ordering combined still exact.
    data = _get_checked(
        organization_owner_client,
        url,
        {"search": "zaphod", "source": "direct", "order_by": "-price_paid", "tier__payment_method": "offline"},
    )
    assert data["count"] == 2


def test_admin_list_ticket_tiers_no_duplicates_with_m2m(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
) -> None:
    """A tier restricted to two membership tiers is listed exactly once."""
    mt1 = MembershipTier.objects.create(organization=event.organization, name="Gold")
    mt2 = MembershipTier.objects.create(organization=event.organization, name="Silver")
    offline_tier.restricted_to_membership_tiers.add(mt1, mt2)

    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    data = _get_checked(organization_owner_client, url)
    tier_ids = [item["id"] for item in data["results"]]
    assert tier_ids.count(str(offline_tier.id)) == 1
