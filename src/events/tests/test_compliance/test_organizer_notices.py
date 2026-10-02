"""Non-blocking organizer notices (AT cash register, DK sales registration, PL kasa fiskalna)."""

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
    ("code", "nexus", "keys", "topic"),
    [
        ("AT", EST, ["at_registrierkasse"], NoticeTopic.OFFLINE_PAYMENT),
        ("AT", VENUE, ["at_registrierkasse"], NoticeTopic.OFFLINE_PAYMENT),
        ("DK", EST, ["dk_sales_registration"], NoticeTopic.OFFLINE_PAYMENT),
        ("DK", VENUE, [], None),  # the duty follows the Danish business, not the venue
        # Online sales too, so it sits with ticket sales, not offline payment (#1067).
        ("PL", EST, ["pl_kasa_fiskalna"], NoticeTopic.TICKET_SALES),
        ("PL", VENUE, ["pl_kasa_fiskalna"], NoticeTopic.TICKET_SALES),
        ("DE", ALL_NEXUS, [], None),  # default: none
        ("IT", ALL_NEXUS, [], None),
    ],
)
def test_notices_per_country_and_nexus(
    code: str, nexus: frozenset[Nexus], keys: list[str], topic: NoticeTopic | None
) -> None:
    notices = get_policy_for_country(code).organizer_notices(nexus)

    assert [n.key for n in notices] == keys
    assert all(n.applies_to == topic for n in notices)


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


def test_pl_notice_reaches_a_foreign_orgs_event_in_poland(
    client: Client, organization: Organization, public_event: Event
) -> None:
    """A German org's event in Warsaw carries the Polish hint, keyed for the ticket-sales settings."""
    organization.vat_country_code = "DE"
    organization.save(update_fields=["vat_country_code"])
    public_event.vat_country_code = "PL"
    public_event.save(update_fields=["vat_country_code"])

    response = client.get(reverse("api:get_event", kwargs={"event_id": public_event.pk}))

    [notice] = response.json()["compliance"]["notices"]
    assert notice["key"] == "pl_kasa_fiskalna"
    assert notice["applies_to"] == "ticket_sales"
    assert "kasa fiskalna" in notice["message"]
