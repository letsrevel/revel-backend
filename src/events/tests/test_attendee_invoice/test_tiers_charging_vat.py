"""``tiers_charging_vat`` on the organization billing info (#1287): which live tiers block a scheme declaration."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.schema import OrganizationBillingInfoSchema
from events.service import small_business_scheme_service as service

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _get(client: Client, org: Organization) -> dict[str, t.Any]:
    response = client.get(reverse("api:get_billing_info", kwargs={"slug": org.slug}))
    assert response.status_code == 200, response.content
    return t.cast(dict[str, t.Any], response.json())


def _tier(event: Event, name: str, vat_rate: str) -> TicketTier:
    return TicketTier.objects.create(event=event, name=name, price=10, vat_rate=Decimal(vat_rate))


def test_lists_a_live_tier_charging_vat(
    organization: Organization, event: Event, event_ticket_tier: TicketTier, owner_client: Client
) -> None:
    event_ticket_tier.vat_rate = Decimal("19.00")
    event_ticket_tier.save()
    assert _get(owner_client, organization)["tiers_charging_vat"] == [
        {
            "event_id": str(event.pk),
            "event_name": event.name,
            "tier_id": str(event_ticket_tier.pk),
            "tier_name": event_ticket_tier.name,
            "vat_rate": "19.00",
        }
    ]


def test_tiers_of_rsvp_only_events_are_not_listed_and_do_not_block_a_scheme(
    organization: Organization, event: Event, event_ticket_tier: TicketTier, owner_client: Client
) -> None:
    """RSVP-only events sell nothing (#1295), so a legacy taxed tier on one is neither listed nor a blocker."""
    event_ticket_tier.vat_rate = Decimal("19.00")
    event_ticket_tier.save()
    Event.objects.filter(pk=event.pk).update(requires_ticket=False)
    assert _get(owner_client, organization)["tiers_charging_vat"] == []

    Organization.objects.filter(pk=organization.pk).update(
        vat_rate=Decimal("0.00"),
        billing_country_code="DE",
        vat_id="DE811234567",
        vat_id_validated=True,
        vat_country_code="DE",
    )
    response = owner_client.patch(
        reverse("api:update_billing_info", kwargs={"slug": organization.slug}),
        data=orjson.dumps({"small_business_scheme": "de_19"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def test_excludes_zero_rated_ended_cancelled_and_foreign_tiers(
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    owner_client: Client,
    public_user: RevelUser,
) -> None:
    now = timezone.now()
    event_ticket_tier.vat_rate = Decimal("0.00")
    event_ticket_tier.save()
    ended = Event.objects.create(
        organization=organization,
        name="Ended",
        slug="ended",
        start=now - timedelta(days=3),
        end=now - timedelta(days=2),
        status=Event.EventStatus.OPEN,
    )
    cancelled = Event.objects.create(
        organization=organization,
        name="Cancelled",
        slug="cancelled",
        start=now + timedelta(days=1),
        end=now + timedelta(days=2),
        status=Event.EventStatus.CANCELLED,
    )
    other_org = Organization.objects.create(name="Other", slug="other", owner=public_user)
    foreign = Event.objects.create(
        organization=other_org,
        name="Foreign",
        slug="foreign",
        start=now,
        end=now + timedelta(days=1),
        status=Event.EventStatus.OPEN,
    )
    for ev in (ended, cancelled, foreign):
        _tier(ev, "Taxed", "19.00")
    assert _get(owner_client, organization)["tiers_charging_vat"] == []


def test_orders_by_event_start_then_tier_name(organization: Organization, event: Event, owner_client: Client) -> None:
    later = Event.objects.create(
        organization=organization,
        name="Later",
        slug="later",
        start=event.start + timedelta(days=5),
        end=event.start + timedelta(days=6),
        status=Event.EventStatus.OPEN,
    )
    _tier(later, "A", "7.00")
    _tier(event, "Z", "7.00")
    _tier(event, "B", "7.00")
    result = _get(owner_client, organization)["tiers_charging_vat"]
    assert [(r["event_name"], r["tier_name"]) for r in result] == [("Event", "B"), ("Event", "Z"), ("Later", "A")]


def test_patch_returns_the_field_too(
    organization: Organization, event_ticket_tier: TicketTier, owner_client: Client
) -> None:
    event_ticket_tier.vat_rate = Decimal("19.00")
    event_ticket_tier.save()
    response = owner_client.patch(
        reverse("api:update_billing_info", kwargs={"slug": organization.slug}),
        data=orjson.dumps({"billing_name": "Renamed"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert [r["tier_id"] for r in response.json()["tiers_charging_vat"]] == [str(event_ticket_tier.pk)]


def test_the_resolver_costs_one_query_and_the_guard_shares_its_queryset(
    organization: Organization, event: Event
) -> None:
    for name in ("A", "B", "C"):
        _tier(event, name, "19.00")
    with CaptureQueriesContext(connection) as ctx:
        result = OrganizationBillingInfoSchema.resolve_tiers_charging_vat(organization)
    assert len(ctx) == 1
    assert len(result) == 3
    assert service.live_tiers_charging_vat(organization).exists()


def test_empty_once_a_scheme_is_declared_since_tier_rates_are_then_ignored(
    organization: Organization, event_ticket_tier: TicketTier, owner_client: Client
) -> None:
    event_ticket_tier.vat_rate = Decimal("19.00")
    event_ticket_tier.save()
    Organization.objects.filter(pk=organization.pk).update(small_business_scheme="de_19")
    organization.refresh_from_db()
    assert _get(owner_client, organization)["tiers_charging_vat"] == []
    with CaptureQueriesContext(connection) as ctx:
        assert OrganizationBillingInfoSchema.resolve_tiers_charging_vat(organization) == []
    assert len(ctx) == 0
