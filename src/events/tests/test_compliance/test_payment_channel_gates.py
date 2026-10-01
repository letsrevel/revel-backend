"""Italy's online-payment gate: tier create/update and checkout (#1057).

Only the online channel is blocked, and only for events held in Italy. Offline and
at-the-door payments (confirmed by the organizer) keep working exactly as before.
"""

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


def _set_country(org: Organization, country: str) -> Organization:
    """Stripe-connected, billing-complete org established in ``country`` (online tiers need both)."""
    org.vat_country_code = country
    org.billing_name = "Org Legal Entity"
    org.billing_address = "Main Street 1"
    org.stripe_account_id = "acct_test"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save()
    return org


@pytest.fixture
def italian_org(organization: Organization) -> Organization:
    return _set_country(organization, "IT")


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


def _buy(event: Event, tier: TicketTier, user: RevelUser) -> t.Any:
    return BatchTicketService(event, tier, user).create_batch([TicketPurchaseItem(guest_name="Guest")])


class TestTierCreation:
    def test_online_tier_is_refused_for_event_in_italy(
        self, owner_client: Client, italian_org: Organization, open_event: Event
    ) -> None:
        response = _create_tier(owner_client, open_event, {"price": "10.00", "payment_method": "online"})

        assert response.status_code == 422, response.content
        detail = response.json()["detail"]
        assert set(response.json()) == {"detail"}
        assert "Online card payments aren't available for events in Italy" in detail
        assert "payment at the door or by bank transfer" in detail
        assert not TicketTier.objects.filter(event=open_event, name="New tier").exists()

    @pytest.mark.parametrize(
        "payload",
        [
            {"price": "10.00", "payment_method": "offline"},
            {"price": "10.00", "payment_method": "at_the_door"},
            {"price_type": "pwyc", "pwyc_min": "5.00", "payment_method": "offline"},
            {"price": "0.00", "payment_method": "free"},
        ],
    )
    def test_offline_and_free_tiers_are_allowed_in_italy(
        self, owner_client: Client, italian_org: Organization, open_event: Event, payload: dict[str, t.Any]
    ) -> None:
        response = _create_tier(owner_client, open_event, payload)

        assert response.status_code == 200, response.content

    def test_online_tier_is_allowed_elsewhere(
        self, owner_client: Client, organization: Organization, open_event: Event
    ) -> None:
        _set_country(organization, "AT")

        response = _create_tier(owner_client, open_event, {"price": "10.00", "payment_method": "online"})

        assert response.status_code == 200, response.content

    def test_territorial_rule_foreign_org_event_in_italy(
        self, owner_client: Client, organization: Organization, open_event: Event
    ) -> None:
        _set_country(organization, "AT")
        open_event.vat_country_code = "IT"
        open_event.save(update_fields=["vat_country_code"])

        response = _create_tier(owner_client, open_event, {"price": "10.00", "payment_method": "online"})

        assert response.status_code == 422

    def test_territorial_rule_italian_org_event_abroad(
        self, owner_client: Client, italian_org: Organization, open_event: Event
    ) -> None:
        open_event.vat_country_code = "AT"
        open_event.save(update_fields=["vat_country_code"])

        response = _create_tier(owner_client, open_event, {"price": "10.00", "payment_method": "online"})

        assert response.status_code == 200, response.content


class TestTierUpdate:
    def test_offline_tier_cannot_switch_to_online(self, italian_org: Organization, open_event: Event) -> None:
        tier = _tier(open_event, price=Decimal("15.00"), payment_method=TicketTier.PaymentMethod.OFFLINE)

        with pytest.raises(CountryComplianceError):
            ticket_service.update_ticket_tier(
                tier,
                TicketTierUpdateSchema(price=Decimal("15.00"), payment_method=TicketTier.PaymentMethod.ONLINE),  # type: ignore[call-arg]
            )

        tier.refresh_from_db()
        assert tier.payment_method == TicketTier.PaymentMethod.OFFLINE

    def test_pre_gate_online_tier_stays_editable(self, italian_org: Organization, open_event: Event) -> None:
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.ONLINE)

        renamed = ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(name="Renamed"))  # type: ignore[call-arg]
        switched = ticket_service.update_ticket_tier(
            tier,
            TicketTierUpdateSchema(payment_method=TicketTier.PaymentMethod.AT_THE_DOOR),  # type: ignore[call-arg]
        )

        assert renamed.name == "Renamed"
        assert switched.payment_method == TicketTier.PaymentMethod.AT_THE_DOOR


class TestCheckoutGate:
    def test_pre_gate_online_tier_cannot_be_bought(
        self, italian_org: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.ONLINE)

        with pytest.raises(CountryComplianceError, match="Online card payments"):
            _buy(open_event, tier, member_user)

        assert not Ticket.objects.filter(tier=tier).exists()
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_offline_paid_tier_works_as_before(
        self, italian_org: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.OFFLINE)

        tickets = _buy(open_event, tier, member_user)

        assert tickets[0].status == Ticket.TicketStatus.PENDING
        # The organizer's payment-confirmation dashboard flow is untouched.
        confirmed = ticket_service.confirm_ticket_payment(tickets[0])
        assert confirmed.status == Ticket.TicketStatus.ACTIVE

    def test_at_the_door_paid_tier_works_as_before(
        self, italian_org: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        tier = _tier(open_event, price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.AT_THE_DOOR)

        tickets = _buy(open_event, tier, member_user)

        assert tickets[0].status == Ticket.TicketStatus.ACTIVE

    def test_free_tier_can_be_bought(
        self, italian_org: Organization, open_event: Event, member_user: RevelUser
    ) -> None:
        tickets = _buy(open_event, _tier(open_event), member_user)

        assert tickets[0].status == Ticket.TicketStatus.ACTIVE
