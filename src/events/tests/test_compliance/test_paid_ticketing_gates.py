"""Italy's paid-ticketing gate: tier create/update and checkout (#1057)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.exceptions import CountryComplianceError
from events.models import Event, Organization, Ticket, TicketTier
from events.schema import TicketPurchaseItem, TicketTierUpdateSchema
from events.service import ticket_service
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


@pytest.fixture
def italian_org(organization: Organization) -> Organization:
    organization.vat_country_code = "IT"
    organization.save(update_fields=["vat_country_code"])
    return organization


@pytest.fixture
def open_event(organization: Organization) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Gig",
        slug="gig",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=5,
    )


def _tier(event: Event, **kwargs: t.Any) -> TicketTier:
    defaults: dict[str, t.Any] = {
        "event": event,
        "name": "Entry",
        "price": Decimal("0.00"),
        "payment_method": TicketTier.PaymentMethod.FREE,
    }
    return TicketTier.objects.create(**{**defaults, **kwargs})


def _create_tier(client: Client, event: Event, payload: dict[str, t.Any]) -> t.Any:
    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    return client.post(url, data=orjson.dumps({"name": "New tier", **payload}), content_type="application/json")


def _buy(event: Event, tier: TicketTier, user: RevelUser, pwyc: Decimal | None = None) -> t.Any:
    return BatchTicketService(event, tier, user).create_batch(
        [TicketPurchaseItem(guest_name="Guest")], pwyc_amount=pwyc
    )


class TestTierCreation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"price": "10.00", "payment_method": "offline"},
            {"price": "10.00", "payment_method": "at_the_door"},
            {"price_type": "pwyc", "pwyc_min": "5.00", "payment_method": "offline"},
        ],
    )
    def test_paid_tier_is_refused_for_italian_org(
        self, owner_client: Client, italian_org: Organization, open_event: Event, payload: dict[str, t.Any]
    ) -> None:
        response = _create_tier(owner_client, open_event, payload)

        assert response.status_code == 422, response.content
        assert set(response.json()) == {"detail"}
        assert "IT" in response.json()["detail"]
        assert not TicketTier.objects.filter(event=open_event, name="New tier").exists()

    def test_free_tier_is_allowed_for_italian_org(
        self, owner_client: Client, italian_org: Organization, open_event: Event
    ) -> None:
        response = _create_tier(owner_client, open_event, {"price": "0.00", "payment_method": "free"})

        assert response.status_code == 200, response.content

    def test_paid_tier_is_allowed_elsewhere(
        self, owner_client: Client, organization: Organization, open_event: Event
    ) -> None:
        organization.vat_country_code = "AT"
        organization.save(update_fields=["vat_country_code"])

        response = _create_tier(owner_client, open_event, {"price": "10.00", "payment_method": "offline"})

        assert response.status_code == 200, response.content

    def test_physical_event_in_italy_is_gated_for_a_foreign_org(
        self, owner_client: Client, organization: Organization, open_event: Event
    ) -> None:
        organization.vat_country_code = "AT"
        organization.save(update_fields=["vat_country_code"])
        open_event.vat_country_code = "IT"
        open_event.save(update_fields=["vat_country_code"])

        response = _create_tier(owner_client, open_event, {"price": "10.00", "payment_method": "offline"})

        assert response.status_code == 422


class TestTierUpdate:
    def test_free_tier_cannot_become_paid(self, italian_org: Organization, open_event: Event) -> None:
        tier = _tier(open_event)

        with pytest.raises(CountryComplianceError):
            ticket_service.update_ticket_tier(
                tier,
                TicketTierUpdateSchema(price=Decimal("15.00"), payment_method=TicketTier.PaymentMethod.OFFLINE),  # type: ignore[call-arg]
            )

        tier.refresh_from_db()
        assert tier.price == 0

    def test_pre_gate_paid_tier_stays_editable(self, italian_org: Organization, open_event: Event) -> None:
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.OFFLINE)

        updated = ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(name="Renamed"))  # type: ignore[call-arg]

        assert updated.name == "Renamed"


class TestCheckoutGate:
    def test_pre_gate_paid_tier_cannot_be_bought(
        self, italian_org: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.AT_THE_DOOR)

        with pytest.raises(CountryComplianceError):
            _buy(open_event, tier, member_user)

        assert not Ticket.objects.filter(tier=tier).exists()
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_free_tier_can_be_bought(
        self, italian_org: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        tier = _tier(open_event)

        tickets = _buy(open_event, tier, member_user)

        assert len(tickets) == 1
        assert tickets[0].status == Ticket.TicketStatus.ACTIVE

    def test_paid_tier_can_be_bought_elsewhere(
        self, organization: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        organization.vat_country_code = "FR"
        organization.save(update_fields=["vat_country_code"])
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.OFFLINE)

        tickets = _buy(open_event, tier, member_user)

        assert tickets[0].status == Ticket.TicketStatus.PENDING
