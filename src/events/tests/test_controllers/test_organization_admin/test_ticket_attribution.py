"""Org-wide attribution breakdown: GET /organization-admin/{slug}/tickets/attribution (#922)."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier

pytestmark = pytest.mark.django_db

NEWSLETTER = {"utm_source": "newsletter", "utm_medium": "email", "utm_campaign": "spring"}
INSTAGRAM = {"utm_source": "instagram", "utm_medium": "social", "utm_campaign": "spring"}


def _url(organization: Organization) -> str:
    return reverse("api:organization_ticket_attribution_breakdown", kwargs={"slug": organization.slug})


@pytest.fixture
def second_event(organization: Organization) -> Event:
    now = timezone.now()
    return Event.objects.create(
        organization=organization,
        name="Second",
        slug="second",
        start=now + timedelta(days=9),
        end=now + timedelta(days=10),
    )


@pytest.fixture
def foreign_event(nonmember_user: RevelUser) -> Event:
    other_org = Organization.objects.create(name="Other", slug="other", owner=nonmember_user)
    now = timezone.now()
    return Event.objects.create(
        organization=other_org,
        name="Foreign",
        slug="foreign",
        start=now + timedelta(days=9),
        end=now + timedelta(days=10),
    )


@pytest.fixture
def tickets(
    event: Event,
    second_event: Event,
    foreign_event: Event,
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
) -> dict[str, Ticket]:
    tier_a = tier_factory(event=event)
    tier_b = tier_factory(event=second_event, name="Second Tier")
    tier_f = tier_factory(event=foreign_event, name="Foreign Tier")
    return {
        "a_newsletter": ticket_factory(tier=tier_a, attribution=NEWSLETTER),
        "a_direct": ticket_factory(tier=tier_a),
        "b_newsletter": ticket_factory(event=second_event, tier=tier_b, attribution=NEWSLETTER),
        "b_instagram": ticket_factory(event=second_event, tier=tier_b, attribution=INSTAGRAM),
        "b_cancelled": ticket_factory(
            event=second_event, tier=tier_b, attribution=INSTAGRAM, status=Ticket.TicketStatus.CANCELLED
        ),
        "foreign": ticket_factory(event=foreign_event, tier=tier_f, attribution=NEWSLETTER),
    }


def test_groups_across_the_organizations_events_only(
    organization_owner_client: Client, organization: Organization, tickets: dict[str, Ticket]
) -> None:
    response = organization_owner_client.get(_url(organization))
    assert response.status_code == 200, response.content
    assert response.json() == [
        {**NEWSLETTER, "utm_content": None, "count": 2},
        {**INSTAGRAM, "utm_content": None, "count": 1},
        {"utm_source": None, "utm_medium": None, "utm_campaign": None, "utm_content": None, "count": 1},
    ]


def test_event_ids_filter(
    organization_owner_client: Client, organization: Organization, second_event: Event, tickets: dict[str, Ticket]
) -> None:
    response = organization_owner_client.get(_url(organization), {"event_ids": [str(second_event.id)]})
    assert response.status_code == 200, response.content
    assert response.json() == [
        {**INSTAGRAM, "utm_content": None, "count": 1},
        {**NEWSLETTER, "utm_content": None, "count": 1},
    ]


def test_since_filter(
    organization_owner_client: Client, organization: Organization, tickets: dict[str, Ticket]
) -> None:
    old = tickets["a_direct"]
    Ticket.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))
    since = (timezone.now() - timedelta(days=1)).isoformat()

    response = organization_owner_client.get(_url(organization), {"since": since})
    assert response.status_code == 200, response.content
    counts = {(row["utm_source"], row["count"]) for row in response.json()}
    assert counts == {("newsletter", 2), ("instagram", 1)}


def test_empty_organization(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_url(organization))
    assert response.status_code == 200, response.content
    assert response.json() == []


def test_requires_ticket_management_permission(member_client: Client, organization: Organization) -> None:
    response = member_client.get(_url(organization))
    assert response.status_code in (403, 404), response.content
