"""The per-event compliance view matches what the gates enforce (EU layer 1)."""

from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from freezegun import freeze_time

from events.compliance import AttendeeInvoicingCapability, PaymentChannelCapability
from events.compliance.enforcement import event_compliance, payment_channel_decision
from events.models import Event, Organization, TicketTier

pytestmark = pytest.mark.django_db


def _place(
    organization: Organization, event: Event, org_country: str, event_country: str, virtual: bool = False
) -> None:
    organization.vat_country_code = org_country
    organization.save(update_fields=["vat_country_code"])
    event.vat_country_code = event_country
    event.is_virtual = virtual
    event.save(update_fields=["vat_country_code", "is_virtual"])


@pytest.mark.parametrize(
    ("org_country", "event_country", "virtual", "online"),
    [
        ("IT", "IT", False, PaymentChannelCapability.BLOCKED),  # Italian org, event in Italy
        ("AT", "IT", False, PaymentChannelCapability.BLOCKED),  # foreign org, event in Italy
        ("IT", "AT", False, PaymentChannelCapability.ALLOWED),  # Italian org, event abroad
        ("IT", "", True, PaymentChannelCapability.ALLOWED),  # Italian org, virtual event
    ],
)
def test_online_payment_follows_where_the_event_is_held(
    organization: Organization, event: Event, org_country: str, event_country: str, virtual: bool, online: str
) -> None:
    _place(organization, event, org_country, event_country, virtual)

    result = event_compliance(event)

    assert result.online_payment == online
    assert result.offline_payment == PaymentChannelCapability.ALLOWED
    assert result.venue_country == ("" if virtual else event_country)


@pytest.mark.parametrize(
    ("org_country", "event_country", "virtual"),
    [("IT", "IT", False), ("AT", "IT", False), ("IT", "AT", False), ("IT", "", True), ("DE", "DE", False)],
)
def test_event_field_matches_enforcement(
    organization: Organization, event: Event, org_country: str, event_country: str, virtual: bool
) -> None:
    """The flag the frontend reads is exactly the decision tier/checkout enforcement takes."""
    _place(organization, event, org_country, event_country, virtual)

    result = event_compliance(event)

    for method, capability in (
        (TicketTier.PaymentMethod.ONLINE, result.online_payment),
        (TicketTier.PaymentMethod.OFFLINE, result.offline_payment),
        (TicketTier.PaymentMethod.AT_THE_DOOR, result.offline_payment),
    ):
        allowed = payment_channel_decision(organization, method, [event]).allowed
        assert allowed is (capability == PaymentChannelCapability.ALLOWED), method


class TestAttendeeInvoicing:
    @pytest.mark.parametrize(("today", "expected"), [("2026-12-31 12:00", "allowed"), ("2027-01-01 12:00", "blocked")])
    def test_spain_date_gate(self, organization: Organization, event: Event, today: str, expected: str) -> None:
        _place(organization, event, "ES", "ES")

        with freeze_time(today):
            assert event_compliance(event).attendee_invoicing == expected

    def test_foreign_org_event_in_a_venue_bound_country(self, organization: Organization, event: Event) -> None:
        _place(organization, event, "AT", "SI")
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED

    def test_foreign_org_event_in_an_establishment_bound_country(
        self, organization: Organization, event: Event
    ) -> None:
        _place(organization, event, "AT", "HR")
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.ALLOWED

    def test_domestic_b2b_country(self, organization: Organization, event: Event) -> None:
        _place(organization, event, "BE", "BE")
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED_FOR_BUSINESS_BUYERS


def test_event_detail_endpoints_expose_it(client: Client, organization: Organization, public_event: Event) -> None:
    _place(organization, public_event, "DE", "IT")
    TicketTier.objects.create(event=public_event, name="Door", price=Decimal("5"))

    response = client.get(reverse("api:get_event", kwargs={"event_id": public_event.pk}))

    assert response.status_code == 200, response.content
    assert response.json()["compliance"] == {
        "venue_country": "IT",
        "online_payment": "blocked",
        "offline_payment": "allowed",
        "attendee_invoicing": "allowed",
        "notices": [],
    }
