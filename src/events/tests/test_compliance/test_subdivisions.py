"""Subdivisions with their own policy: the Basque Country (TicketBAI) and Navarre (#1086)."""

import datetime
import itertools
import typing as t
from decimal import Decimal
from unittest.mock import patch

import orjson
import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import ImproperlyConfigured
from django.test.client import Client
from django.urls import reverse
from freezegun import freeze_time

from accounts.models import RevelUser
from events.compliance import (
    AttendeeInvoicingCapability,
    BuyerContext,
    DefaultEUPolicy,
    get_policy,
    get_policy_for_country,
    register,
    registry,
    resolve_org_jurisdiction,
)
from events.compliance.base import ALL_NEXUS, ESTABLISHMENT_ONLY, Nexus
from events.compliance.enforcement import event_compliance, sale_nexus
from events.compliance.policies.es import BasqueCountryPolicy, NavarrePolicy, SpainPolicy
from events.exceptions import CountryComplianceError
from events.models import Event, Organization, Payment, Refund, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_draft_service import issue_draft_invoice
from events.service.attendee_invoice_service import (
    generate_attendee_credit_note,
    generate_attendee_invoice,
    set_invoicing_mode,
)
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_draft,
    _create_issued,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from geo.models import City

pytestmark = pytest.mark.django_db

BEFORE_2027 = "2026-10-02 12:00"
AFTER_2027 = "2027-01-01 12:00"
_city_ids = itertools.count(95001)


def _city(name: str, iso2: str, admin_name: str | None) -> City:
    """A city row: CI loads only the mini fixture, which has no Basque or Navarre city."""
    return City.objects.create(
        name=name,
        ascii_name=name,
        country=name,
        iso2=iso2,
        iso3=f"{iso2}X",
        admin_name=admin_name,
        city_id=next(_city_ids),
        location=Point(-2.92, 43.26),
    )


def _place(org: Organization, vat_country: str, city: City | None) -> Organization:
    """An invoicing-ready org with ``vat_country`` declared and ``city`` set."""
    _make_org_invoicing_ready(org)
    org.vat_country_code = vat_country
    org.vat_id = f"{vat_country}123456789" if vat_country else ""
    org.city = city
    org.save()
    return org


def _basque(org: Organization) -> Organization:
    return _place(org, "ES", _city("Bilbao", "ES", "Basque Country"))


def _consumer() -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": "", "vat_country_code": "", "billing_name": "Jane Doe"}


class TestJurisdictionResolution:
    @pytest.mark.parametrize(
        ("vat_country", "city", "expected"),
        [
            ("ES", ("Bilbao", "ES", "Basque Country"), "ES-PV"),
            ("ES", ("Vitoria", "ES", "País Vasco"), "ES-PV"),
            ("ES", ("Donostia", "ES", "  euskadi "), "ES-PV"),
            ("ES", ("Pamplona", "ES", "Navarre"), "ES-NC"),
            ("ES", ("Tudela", "ES", "Navarra"), "ES-NC"),
            ("ES", ("Madrid", "ES", "Madrid"), "ES"),
            ("ES", ("Somewhere", "ES", None), "ES"),
            ("ES", None, "ES"),
            # The declared country wins; a city elsewhere never refines it.
            ("FR", ("Bilbao", "ES", "Basque Country"), "FR"),
            # No declared country: the city gives both the country and the subdivision.
            ("", ("Bilbao", "ES", "Basque Country"), "ES-PV"),
            ("", None, ""),
        ],
    )
    def test_resolution(
        self,
        organization: Organization,
        vat_country: str,
        city: tuple[str, str, str | None] | None,
        expected: str,
    ) -> None:
        _place(organization, vat_country, _city(*city) if city else None)

        assert resolve_org_jurisdiction(organization) == expected
        policy = get_policy(organization)
        assert (policy.jurisdiction, policy.country) == (expected, expected[:2])

    def test_establishment_is_keyed_by_jurisdiction_and_venue_by_country(
        self, organization: Organization, event: Event
    ) -> None:
        _basque(organization)
        event.vat_country_code = "ES"
        event.is_virtual = False
        event.save(update_fields=["vat_country_code", "is_virtual"])

        assert sale_nexus(organization, [event]) == {
            "ES-PV": frozenset({Nexus.ESTABLISHMENT}),
            "ES": frozenset({Nexus.VENUE}),
        }


class TestRegistry:
    def test_subdivision_falls_back_to_its_country(self) -> None:
        policy = get_policy_for_country("es-xx")
        assert type(policy) is SpainPolicy
        assert (policy.jurisdiction, policy.country) == ("ES-XX", "ES")

    def test_unregistered_country_subdivision_gets_the_default(self) -> None:
        assert type(get_policy_for_country("DE-BY")) is DefaultEUPolicy

    def test_exact_subdivision_wins(self) -> None:
        assert type(get_policy_for_country("ES-PV")) is BasqueCountryPolicy
        assert type(get_policy_for_country("ES-NC")) is NavarrePolicy

    @pytest.mark.parametrize(
        ("code", "admin_names"),
        [("ES-", ()), ("ES-ABCD", ("X",)), ("E-PV", ("X",)), ("ES-QQ", ()), ("XK", ("Somewhere",))],
    )
    def test_malformed_registration_is_refused(self, code: str, admin_names: tuple[str, ...]) -> None:
        with pytest.raises(ImproperlyConfigured):
            register(code, admin_names=admin_names)

    def test_duplicate_subdivision_is_refused(self) -> None:
        with pytest.raises(ImproperlyConfigured):

            @register("ES-PV", admin_names=("Basque Country",))
            class _Another(DefaultEUPolicy):
                pass

    def test_a_new_subdivision_needs_only_a_registration(
        self, monkeypatch: pytest.MonkeyPatch, organization: Organization
    ) -> None:
        monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
        monkeypatch.setattr(registry, "_SUBDIVISIONS", dict(registry._SUBDIVISIONS))

        @register("IT-32", admin_names=("Trentino-Alto Adige",))
        class _TrentinoPolicy(DefaultEUPolicy):
            pass

        _place(organization, "IT", _city("Trento", "IT", "Trentino-Alto Adige"))
        assert isinstance(get_policy(organization), _TrentinoPolicy)


class TestBasqueCountryPolicy:
    @pytest.mark.parametrize("today", [BEFORE_2027, AFTER_2027])
    def test_blocked_from_today_unlike_the_rest_of_spain(self, today: str) -> None:
        with freeze_time(today):
            basque = get_policy_for_country("ES-PV")
            assert basque.attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED
            assert basque.organizer_notices(ALL_NEXUS) == []
            spain = get_policy_for_country("ES").attendee_invoicing_capability()
            assert (spain == AttendeeInvoicingCapability.ALLOWED) is (today == BEFORE_2027)

    def test_refusal_names_the_basque_country_and_ticketbai(self) -> None:
        decision = get_policy_for_country("ES-PV").attendee_invoicing(BuyerContext(), ESTABLISHMENT_ONLY)

        assert not decision.allowed
        assert "Basque Country" in decision.reason and "TicketBAI" in decision.reason and "Batuz" in decision.reason
        assert "Verifactu" not in decision.reason
        # The skipped-document record keeps the ISO 3166-1 country.
        assert (decision.code, decision.country) == ("fiscalized_invoicing", "ES")

    def test_establishment_only(self) -> None:
        assert get_policy_for_country("ES-PV").attendee_invoicing(BuyerContext(), frozenset({Nexus.VENUE})).allowed


class TestNavarrePolicy:
    def test_keeps_the_2027_block_without_claiming_verifactu(self) -> None:
        policy = get_policy_for_country("ES-NC")
        with freeze_time(BEFORE_2027):
            assert policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.ALLOWED
            notices = policy.organizer_notices(ALL_NEXUS)
        with freeze_time(AFTER_2027):
            decision = policy.attendee_invoicing(BuyerContext(), ESTABLISHMENT_ONLY)
            assert policy.organizer_notices(ALL_NEXUS) == []

        assert [n.key for n in notices] == ["es_nc_naticket"]
        assert not decision.allowed
        for text in (notices[0].message, decision.reason):
            assert "Navarre" in text and "NaTicket" in text and "Verifactu" not in text
        assert NavarrePolicy.fiscal_invoicing_from == SpainPolicy.fiscal_invoicing_from


class TestOrganizationPayload:
    def _compliance(self, client: Client, org: Organization) -> dict[str, t.Any]:
        # Only the policy's "today" moves: freezing the clock would expire the client's JWT.
        with patch("events.compliance.base.timezone.localdate", return_value=datetime.date(2026, 10, 2)):
            response = client.get(reverse("api:get_organization_admin", kwargs={"slug": org.slug}))
        assert response.status_code == 200
        return t.cast(dict[str, t.Any], response.json()["compliance"])

    def test_basque_org_has_region_blocked_invoicing_and_no_spain_notice(
        self, owner_client: Client, organization: Organization
    ) -> None:
        _basque(organization)

        compliance = self._compliance(owner_client, organization)

        assert (compliance["country"], compliance["region"]) == ("ES", "ES-PV")
        assert compliance["attendee_invoicing"] == "blocked"
        assert compliance["notices"] == []

    def test_navarre_org_has_its_own_notice(self, owner_client: Client, organization: Organization) -> None:
        _place(organization, "ES", _city("Pamplona", "ES", "Navarre"))

        compliance = self._compliance(owner_client, organization)

        assert (compliance["country"], compliance["region"]) == ("ES", "ES-NC")
        assert [n["key"] for n in compliance["notices"]] == ["es_nc_naticket"]

    def test_other_orgs_have_an_empty_region(self, owner_client: Client, organization: Organization) -> None:
        _place(organization, "ES", _city("Madrid", "ES", "Madrid"))

        compliance = self._compliance(owner_client, organization)

        assert (compliance["country"], compliance["region"]) == ("ES", "")
        assert [n["key"] for n in compliance["notices"]] == ["es_verifactu"]

    def test_basque_org_event_is_blocked_without_notice(self, organization: Organization, event: Event) -> None:
        _basque(organization)

        with freeze_time(BEFORE_2027):
            compliance = event_compliance(event)

        assert compliance.attendee_invoicing == AttendeeInvoicingCapability.BLOCKED
        assert compliance.notices == []


class TestEnablingInvoicing:
    def test_service_refuses_with_ticketbai_copy(self, organization: Organization) -> None:
        _basque(organization)

        for mode in (Organization.InvoicingMode.HYBRID, Organization.InvoicingMode.AUTO):
            with pytest.raises(CountryComplianceError, match="TicketBAI"):
                set_invoicing_mode(organization, mode)

        organization.refresh_from_db()
        assert organization.invoicing_mode == Organization.InvoicingMode.NONE

    def test_endpoint_returns_422_with_ticketbai_detail(self, owner_client: Client, organization: Organization) -> None:
        _basque(organization)
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})

        response = owner_client.patch(url, data=orjson.dumps({"mode": "hybrid"}), content_type="application/json")

        assert response.status_code == 422
        assert set(response.json()) == {"detail"}
        assert "Basque Country" in response.json()["detail"] and "TicketBAI" in response.json()["detail"]


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
class TestSkippedDocuments:
    """A Basque org that enabled invoicing anyway gets nothing issued, and every skip is recorded."""

    def _sale(self, org: Organization, event: Event, tier: TicketTier, user: RevelUser) -> Payment:
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save(update_fields=["invoicing_mode"])
        return _create_payment(user=user, event=event, tier=tier, buyer_billing_snapshot=_consumer())

    @freeze_time(BEFORE_2027)
    def test_generation_is_skipped_and_recorded(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        self._sale(_basque(organization), event, event_ticket_tier, member_user)

        assert generate_attendee_invoice("cs_test_123") is None

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.kind, doc.reason_code, doc.policy_country) == (
            SkippedFiscalDocument.Kind.INVOICE,
            SkippedFiscalDocument.ReasonCode.FISCALIZED_INVOICING,
            "ES",
        )
        assert "TicketBAI" in doc.reason

    def test_draft_cannot_be_issued(
        self, _pdf: t.Any, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        draft = _create_draft(_basque(organization), event, member_user)

        with pytest.raises(CountryComplianceError, match="TicketBAI"):
            issue_draft_invoice(draft)

        draft.refresh_from_db()
        assert draft.status == AttendeeInvoice.InvoiceStatus.DRAFT

    def test_credit_note_is_skipped_and_recorded(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        payment = self._sale(_basque(organization), event, event_ticket_tier, member_user)
        invoice = _create_issued(organization, event, member_user)
        invoice.stripe_session_id = payment.stripe_session_id
        invoice.save(update_fields=["stripe_session_id"])
        refund = Refund.objects.create(
            payment=payment,
            amount=Decimal("100.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )

        assert generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[refund.id]) is None

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.kind, doc.policy_country, doc.invoice) == (SkippedFiscalDocument.Kind.CREDIT_NOTE, "ES", invoice)
        assert not invoice.credit_notes.exists()
