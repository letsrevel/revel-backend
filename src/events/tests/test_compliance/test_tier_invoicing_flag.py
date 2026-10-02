"""The tier ``invoicing_available`` flag follows the per-event invoicing decision (#1107)."""

import typing as t

import pytest
from django.contrib.gis.geos import Point
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from events.models import Event, Organization, TicketTier, Venue
from events.schema import TicketTierDetailSchema, TicketTierSchema
from geo.models import City

pytestmark = pytest.mark.django_db


def _city(iso2: str, city_id: int) -> City:
    return City.objects.create(
        name=f"City {iso2}", ascii_name=f"City {iso2}", country=iso2, iso2=iso2, city_id=city_id, location=Point(0, 0)
    )


@pytest.fixture
def placed_event(organization: Organization, event: Event) -> Event:
    """An Austrian org (by city) with invoicing on, its event at a Greek venue."""
    organization.city = _city("AT", 1)
    organization.invoicing_mode = Organization.InvoicingMode.AUTO
    organization.save(update_fields=["city", "invoicing_mode"])
    event.venue = Venue.objects.create(organization=organization, name="Venue", city=_city("GR", 2))
    event.save(update_fields=["venue"])
    return event


def _tier(event: Event, name: str = "General") -> TicketTier:
    return TicketTier.objects.create(event=event, name=name, price=10, payment_method=TicketTier.PaymentMethod.ONLINE)


def test_event_in_a_venue_bound_country_has_no_invoices(placed_event: Event) -> None:
    """Greece blocks Revel invoices for events held there, whoever organizes them."""
    tier = _tier(placed_event)

    assert TicketTierSchema.resolve_invoicing_available(tier) is False
    assert TicketTierDetailSchema.resolve_invoicing_available(tier) is False


def test_domestic_event_has_invoices(placed_event: Event) -> None:
    placed_event.venue.city = placed_event.organization.city  # type: ignore[union-attr]
    placed_event.venue.save(update_fields=["city"])  # type: ignore[union-attr]
    tier = _tier(placed_event)

    assert TicketTierSchema.resolve_invoicing_available(tier) is True
    assert TicketTierDetailSchema.resolve_invoicing_available(tier) is True


def test_invoicing_mode_none_has_no_invoices(placed_event: Event) -> None:
    placed_event.venue = None
    placed_event.save(update_fields=["venue"])
    placed_event.organization.invoicing_mode = Organization.InvoicingMode.NONE
    placed_event.organization.save(update_fields=["invoicing_mode"])

    assert TicketTierSchema.resolve_invoicing_available(_tier(placed_event)) is False


def test_offline_tier_has_no_invoices(placed_event: Event) -> None:
    placed_event.venue = None
    placed_event.save(update_fields=["venue"])
    tier = _tier(placed_event)
    tier.payment_method = TicketTier.PaymentMethod.OFFLINE

    assert TicketTierSchema.resolve_invoicing_available(tier) is False


def test_built_schema_keeps_its_value(placed_event: Event) -> None:
    """Ninja's re-validation pass hands the resolver an assembled schema: pass it through."""
    built = TicketTierSchema.from_orm(_tier(placed_event))

    assert TicketTierSchema.resolve_invoicing_available(built) is False


def _count(client: Client, url: str) -> tuple[int, t.Any]:
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return len(ctx.captured_queries), response.json()


@pytest.mark.parametrize("admin", [False, True])
def test_tier_list_query_count_is_stable(placed_event: Event, owner_client: Client, admin: bool) -> None:
    """Resolving the flag costs no extra query per tier on the tier lists."""
    url_name = "api:list_ticket_tiers" if admin else "api:tier_list"
    url = reverse(url_name, kwargs={"event_id": placed_event.pk})
    _tier(placed_event)
    _count(owner_client, url)  # warm up per-request caches
    baseline, before = _count(owner_client, url)

    for i in range(3):
        _tier(placed_event, name=f"Tier {i}")
    scaled, body = _count(owner_client, url)

    tiers = body["results"] if admin else body
    assert len(tiers) == len(before["results"] if admin else before) + 3
    assert all(tier["invoicing_available"] is False for tier in tiers)
    assert scaled == baseline
