"""Organizer-facing attribution: admin list field + filters, and the breakdown endpoint (#922)."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Ticket, TicketTier
from events.schema import UserTicketSchema

pytestmark = pytest.mark.django_db

NEWSLETTER = {"utm_source": "newsletter", "utm_medium": "email", "utm_campaign": "spring"}
INSTAGRAM = {"utm_source": "instagram", "utm_medium": "social", "utm_campaign": "spring"}


@pytest.fixture
def tagged_tickets(
    ticket_factory: t.Callable[..., Ticket], tier_factory: t.Callable[..., TicketTier]
) -> dict[str, Ticket]:
    tier = tier_factory()
    return {
        "newsletter": ticket_factory(tier=tier, attribution=NEWSLETTER),
        "instagram_a": ticket_factory(tier=tier, attribution=INSTAGRAM),
        "instagram_b": ticket_factory(tier=tier, attribution=INSTAGRAM),
        "instagram_cancelled": ticket_factory(tier=tier, attribution=INSTAGRAM, status=Ticket.TicketStatus.CANCELLED),
        "direct": ticket_factory(tier=tier),
    }


class TestAdminList:
    def test_field_is_exposed(
        self, organization_owner_client: Client, event: Event, tagged_tickets: dict[str, Ticket]
    ) -> None:
        url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
        response = organization_owner_client.get(url)
        assert response.status_code == 200, response.content
        by_id = {row["id"]: row["attribution"] for row in response.json()["results"]}
        assert by_id[str(tagged_tickets["newsletter"].id)] == NEWSLETTER
        assert by_id[str(tagged_tickets["direct"].id)] is None

    def test_filter_by_source(
        self, organization_owner_client: Client, event: Event, tagged_tickets: dict[str, Ticket]
    ) -> None:
        url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
        response = organization_owner_client.get(url, {"utm_source": "instagram"})
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {
            str(tagged_tickets["instagram_a"].id),
            str(tagged_tickets["instagram_b"].id),
            str(tagged_tickets["instagram_cancelled"].id),
        }

    def test_filter_by_campaign(
        self, organization_owner_client: Client, event: Event, tagged_tickets: dict[str, Ticket]
    ) -> None:
        url = reverse("api:list_tickets", kwargs={"event_id": event.pk})
        response = organization_owner_client.get(url, {"utm_campaign": "spring", "utm_source": "newsletter"})
        assert response.status_code == 200, response.content
        assert [row["id"] for row in response.json()["results"]] == [str(tagged_tickets["newsletter"].id)]

    def test_user_facing_schema_does_not_expose_it(self) -> None:
        assert "attribution" not in UserTicketSchema.model_fields


class TestBreakdown:
    def _url(self, event: Event) -> str:
        return reverse("api:ticket_attribution_breakdown", kwargs={"event_id": event.pk})

    def test_groups_non_cancelled_tickets_with_a_direct_bucket(
        self, organization_owner_client: Client, event: Event, tagged_tickets: dict[str, Ticket]
    ) -> None:
        response = organization_owner_client.get(self._url(event))
        assert response.status_code == 200, response.content
        assert response.json() == [
            {**INSTAGRAM, "utm_content": None, "count": 2},
            {**NEWSLETTER, "utm_content": None, "count": 1},
            {"utm_source": None, "utm_medium": None, "utm_campaign": None, "utm_content": None, "count": 1},
        ]

    def test_empty_event(self, organization_owner_client: Client, event: Event) -> None:
        response = organization_owner_client.get(self._url(event))
        assert response.status_code == 200, response.content
        assert response.json() == []

    def test_requires_organizer(self, member_client: Client, event: Event) -> None:
        response = member_client.get(self._url(event))
        assert response.status_code in (403, 404), response.content
