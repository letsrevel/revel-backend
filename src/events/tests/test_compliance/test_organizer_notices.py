"""Non-blocking organizer notices (AT, DK, PL; HR/SI/GR/HU fiscal invoicing; the ES Verifactu heads-up)."""

import datetime
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from freezegun import freeze_time

from events.compliance import Nexus, NoticeTopic, get_policy_for_country
from events.compliance.base import ALL_NEXUS
from events.compliance.enforcement import event_compliance, payment_channel_decision
from events.compliance.policies.es import UPCOMING_BLOCK_NOTICE, SpainPolicy
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
        # Revel can't issue attendee invoices: scoped like each country's invoicing block (#1092).
        ("HR", EST, ["hr_fiscalization"], NoticeTopic.ATTENDEE_INVOICING),
        ("HR", VENUE, [], None),  # the Croatian block follows the business, not the venue
        ("SI", EST, ["si_furs"], NoticeTopic.ATTENDEE_INVOICING),
        ("SI", VENUE, ["si_furs"], NoticeTopic.ATTENDEE_INVOICING),
        ("GR", EST, ["gr_mydata"], NoticeTopic.ATTENDEE_INVOICING),
        ("GR", VENUE, ["gr_mydata"], NoticeTopic.ATTENDEE_INVOICING),
        ("HU", EST, ["hu_nav"], NoticeTopic.ATTENDEE_INVOICING),
        ("HU", VENUE, ["hu_nav"], NoticeTopic.ATTENDEE_INVOICING),
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


def test_si_notice_on_org_card_and_foreign_orgs_event_in_slovenia(
    owner_client: Client, client: Client, organization: Organization, public_event: Event
) -> None:
    """The FURS hint sits next to the invoicing setting, for SI orgs and for any event held in Slovenia."""
    expected = [
        {
            "key": "si_furs",
            "applies_to": "attendee_invoicing",
            "message": "Revel can't issue attendee invoices where Slovenian rules apply: invoices for card and online "
            "payments, which under FURS guidance generally include payments through Stripe, must be verified with "
            "FURS in real time. If you must issue invoices, issue a FURS-verified invoice for every paid sale from "
            "your own software, even with attendee invoicing turned off.",
        }
    ]
    organization.vat_country_code = "SI"
    organization.save(update_fields=["vat_country_code"])
    org_response = owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))
    assert org_response.json()["compliance"]["notices"] == expected

    organization.vat_country_code = "DE"
    organization.save(update_fields=["vat_country_code"])
    public_event.vat_country_code = "SI"
    public_event.save(update_fields=["vat_country_code"])
    event_response = client.get(reverse("api:get_event", kwargs={"event_id": public_event.pk}))
    assert event_response.json()["compliance"]["notices"] == expected


def test_hr_notice_follows_the_croatian_organizer(organization: Organization, event: Event) -> None:
    """A Croatian org's event abroad keeps the HR hint; a foreign org's event in Croatia gets none."""
    organization.vat_country_code = "HR"
    organization.save(update_fields=["vat_country_code"])
    event.vat_country_code = "DE"
    event.save(update_fields=["vat_country_code"])
    assert [n.key for n in event_compliance(event).notices] == ["hr_fiscalization"]

    organization.vat_country_code = "DE"
    organization.save(update_fields=["vat_country_code"])
    event.vat_country_code = "HR"
    event.save(update_fields=["vat_country_code"])
    assert event_compliance(event).notices == []


class TestSpainUpcomingBlockNotice:
    """Spain warns before the Verifactu block (#1087); from that date the block itself explains."""

    @pytest.mark.parametrize(
        ("today", "keys"),
        [
            ("2026-12-31 12:00", ["es_verifactu"]),
            ("2027-01-01 12:00", []),
            ("2027-07-01 12:00", []),
        ],
    )
    def test_shown_only_before_the_block(self, today: str, keys: list[str]) -> None:
        """Present on the last day before the block, gone from its first day and after the July date."""
        with freeze_time(today):
            notices = get_policy_for_country("ES").organizer_notices(ALL_NEXUS)

        assert [n.key for n in notices] == keys
        assert all(n.applies_to == NoticeTopic.ATTENDEE_INVOICING for n in notices)

    @freeze_time("2026-12-31 12:00")
    def test_follows_the_spanish_organizer_only(self) -> None:
        """Like the block, it reaches organizers established in Spain, not events held there."""
        policy = get_policy_for_country("ES")

        assert [n.key for n in policy.organizer_notices(EST)] == ["es_verifactu"]
        assert policy.organizer_notices(VENUE) == []

    def test_copy_names_the_block_date(self) -> None:
        """The message spells out the date the gate uses; keep the two together."""
        assert SpainPolicy.fiscal_invoicing_from == datetime.date(2027, 1, 1)
        assert "1 January 2027" in str(UPCOMING_BLOCK_NOTICE)

    def test_org_card_payload(self, owner_client: Client, organization: Organization) -> None:
        """A Spanish org card reads `allowed` today and carries the heads-up next to the invoicing setting."""
        organization.vat_country_code = "ES"
        organization.save(update_fields=["vat_country_code"])

        # Only the policy's "today" moves: freezing the clock would expire the client's JWT.
        with patch("events.compliance.base.timezone.localdate", return_value=datetime.date(2026, 10, 2)):
            response = owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))

        assert response.json()["compliance"]["attendee_invoicing"] == "allowed"
        assert response.json()["compliance"]["notices"] == [
            {"key": "es_verifactu", "applies_to": "attendee_invoicing", "message": str(UPCOMING_BLOCK_NOTICE)}
        ]
