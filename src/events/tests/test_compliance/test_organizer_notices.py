"""Non-blocking organizer notices (AT cash register, DK sales registration)."""

import pytest
from django.test.client import Client
from django.urls import reverse

from events.compliance import Nexus, NoticeTopic, get_policy_for_country
from events.compliance.base import ALL_NEXUS
from events.compliance.enforcement import event_compliance, payment_channel_decision
from events.models import Event, Organization, TicketTier

pytestmark = pytest.mark.django_db

EST = frozenset({Nexus.ESTABLISHMENT})
VENUE = frozenset({Nexus.VENUE})


@pytest.mark.parametrize(
    ("code", "nexus", "keys"),
    [
        ("AT", EST, ["at_registrierkasse"]),
        ("AT", VENUE, ["at_registrierkasse"]),
        ("DK", EST, ["dk_sales_registration"]),
        ("DK", VENUE, []),  # the duty follows the Danish business, not the venue
        ("DE", ALL_NEXUS, []),  # default: none
        ("IT", ALL_NEXUS, []),
    ],
)
def test_notices_per_country_and_nexus(code: str, nexus: frozenset[Nexus], keys: list[str]) -> None:
    notices = get_policy_for_country(code).organizer_notices(nexus)

    assert [n.key for n in notices] == keys
    assert all(n.applies_to == NoticeTopic.OFFLINE_PAYMENT for n in notices)


def test_notices_never_block_anything(organization: Organization, event: Event) -> None:
    organization.vat_country_code = "AT"
    organization.save(update_fields=["vat_country_code"])

    for method in (
        TicketTier.PaymentMethod.OFFLINE,
        TicketTier.PaymentMethod.AT_THE_DOOR,
        TicketTier.PaymentMethod.ONLINE,
    ):
        assert payment_channel_decision(organization, method, [event]).allowed


def test_event_notices_follow_the_venue(organization: Organization, event: Event) -> None:
    """A German org's event in Vienna gets the Austrian hint; a Danish org's event abroad keeps the DK one."""
    organization.vat_country_code = "DE"
    organization.save(update_fields=["vat_country_code"])
    event.vat_country_code = "AT"
    event.save(update_fields=["vat_country_code"])
    assert [n.key for n in event_compliance(event).notices] == ["at_registrierkasse"]

    organization.vat_country_code = "DK"
    organization.save(update_fields=["vat_country_code"])
    event.vat_country_code = "AT"
    event.save(update_fields=["vat_country_code"])
    assert {n.key for n in event_compliance(event).notices} == {"dk_sales_registration", "at_registrierkasse"}


def test_org_and_event_payloads_expose_notices(
    owner_client: Client, client: Client, organization: Organization, public_event: Event
) -> None:
    organization.vat_country_code = "AT"
    organization.save(update_fields=["vat_country_code"])
    expected = [
        {
            "key": "at_registrierkasse",
            "applies_to": "offline_payment",
            "message": "Payments you take at the door go through your own registered cash register "
            "(Registrierkasse) once you pass the legal thresholds. Revel's online sales are exempt.",
        }
    ]

    org_response = owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))
    event_response = client.get(reverse("api:get_event", kwargs={"event_id": public_event.pk}))

    assert org_response.json()["compliance"]["notices"] == expected
    assert event_response.json()["compliance"]["notices"] == expected
