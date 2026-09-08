"""The tier admin schema carries per-platform sold counts read through the reverse relation."""

import typing as t
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from integrations.models import PlatformConnection, TierLink
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def connected(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> PlatformConnection:
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


def test_tier_list_exposes_external_sales(
    organization_owner_client: Client,
    event: Event,
    connected: PlatformConnection,
    django_assert_max_num_queries: t.Any,
) -> None:
    event.ticket_tiers.all().delete()
    ga = TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    TicketTier.objects.create(
        event=event, name="Offline", price=Decimal("5"), total_quantity=10
    )  # skipped by the mapper → no link
    link = sync_service.push_link(sync_service.ensure_link(event, connected))
    TierLink.objects.filter(event_link=link, tier=ga).update(
        remote_quantity_sold=12, counts_updated_at=timezone.now(), remote_paused=True
    )
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.id})
    response = organization_owner_client.get(url)
    assert response.status_code == 200, response.content
    rows = {r["name"]: r for r in response.json()["results"]}
    assert rows["GA"]["external_sales"] == [
        {
            "provider": "fake",
            "quantity_sold": 12,
            "updated_at": rows["GA"]["external_sales"][0]["updated_at"],
            "paused": True,
        }
    ]
    assert rows["GA"]["external_sales"][0]["updated_at"] is not None
    assert rows["Offline"]["external_sales"] == []


def test_no_extra_queries_per_tier(
    organization_owner_client: Client,
    event: Event,
    connected: PlatformConnection,
    django_assert_num_queries: t.Any,
) -> None:
    event.ticket_tiers.all().delete()
    for i in range(5):
        TicketTier.objects.create(
            event=event,
            name=f"T{i}",
            price=Decimal("10"),
            total_quantity=10,
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
    sync_service.push_link(sync_service.ensure_link(event, connected))
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.id})
    organization_owner_client.get(url)  # warm any per-request caches
    from django.db import connection as db
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(db) as ctx:
        organization_owner_client.get(url)
    tier_link_queries = [q for q in ctx.captured_queries if "integrations_tierlink" in q["sql"]]
    assert len(tier_link_queries) == 1  # one prefetch, not one per tier
