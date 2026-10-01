"""Attendee-invoicing gates per country (#1058-#1060, #1062-#1067)."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from freezegun import freeze_time

from accounts.models import RevelUser
from events.compliance.base import country_name
from events.exceptions import CountryComplianceError
from events.models import Event, Organization, TicketTier
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

pytestmark = pytest.mark.django_db


def _ready_in(org: Organization, country: str, mode: Organization.InvoicingMode) -> Organization:
    """An invoicing-ready org established in ``country`` with ``mode`` already stored."""
    _make_org_invoicing_ready(org)
    org.vat_country_code = country
    org.vat_id = f"{country}123456789"
    org.invoicing_mode = mode
    org.save()
    return org


def _consumer_snapshot() -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": "", "vat_country_code": "", "billing_name": "Jane Doe"}


def _business_snapshot(vat_id: str) -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": vat_id, "vat_country_code": vat_id[:2]}


class TestEnablingInvoicing:
    @pytest.mark.parametrize("country", ["HR", "PT", "SI", "GR", "RO", "HU"])
    def test_blocked_countries_cannot_enable(self, organization: Organization, country: str) -> None:
        _ready_in(organization, country, Organization.InvoicingMode.NONE)

        for mode in (Organization.InvoicingMode.HYBRID, Organization.InvoicingMode.AUTO):
            with pytest.raises(CountryComplianceError, match=country_name(country)):
                set_invoicing_mode(organization, mode)

        organization.refresh_from_db()
        assert organization.invoicing_mode == Organization.InvoicingMode.NONE

    @pytest.mark.parametrize("country", ["BE", "PL", "IT", "DE", "ES"])
    def test_other_countries_can_enable(self, organization: Organization, country: str) -> None:
        _ready_in(organization, country, Organization.InvoicingMode.NONE)

        set_invoicing_mode(organization, Organization.InvoicingMode.AUTO)

        assert organization.invoicing_mode == Organization.InvoicingMode.AUTO

    def test_none_is_always_allowed(self, organization: Organization) -> None:
        _ready_in(organization, "HR", Organization.InvoicingMode.HYBRID)

        set_invoicing_mode(organization, Organization.InvoicingMode.NONE)

        assert organization.invoicing_mode == Organization.InvoicingMode.NONE

    def test_endpoint_returns_translated_422_detail(self, owner_client: Client, organization: Organization) -> None:
        _ready_in(organization, "PT", Organization.InvoicingMode.NONE)
        url = reverse("api:set_invoicing_mode", kwargs={"slug": organization.slug})

        response = owner_client.patch(url, data=orjson.dumps({"mode": "auto"}), content_type="application/json")

        assert response.status_code == 422
        body = response.json()
        assert set(body) == {"detail"}
        assert "Portugal" in body["detail"]


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
class TestGenerationGate:
    def test_blocked_country_skips_generation_for_pre_gate_mode(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """An org that enabled invoicing before the gate gets no invoice, and nothing crashes."""
        _ready_in(organization, "RO", Organization.InvoicingMode.AUTO)
        _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_consumer_snapshot()
        )

        assert generate_attendee_invoice("cs_test_123") is None
        assert not AttendeeInvoice.objects.exists()

    def test_unrestricted_org_selling_into_a_blocked_country_is_skipped(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Physical admission is taxed where the event is: an AT org's event in HU is gated too."""
        _ready_in(organization, "AT", Organization.InvoicingMode.AUTO)
        event.vat_country_code = "HU"
        event.save(update_fields=["vat_country_code"])
        _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_consumer_snapshot()
        )

        assert generate_attendee_invoice("cs_test_123") is None

    def test_foreign_org_selling_into_an_establishment_only_country_is_invoiced(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Croatian fiscalization binds Croatian taxpayers, not an AT org's event in Zagreb (minimal scope)."""
        _ready_in(organization, "AT", Organization.InvoicingMode.AUTO)
        event.vat_country_code = "HR"
        event.save(update_fields=["vat_country_code"])
        _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_consumer_snapshot()
        )

        assert generate_attendee_invoice("cs_test_123") is not None

    @pytest.mark.parametrize(("today", "issued"), [("2026-10-01", True), ("2027-01-01 12:00", False)])
    def test_spain_is_gated_from_2027(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        today: str,
        issued: bool,
    ) -> None:
        _ready_in(organization, "ES", Organization.InvoicingMode.AUTO)
        _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_consumer_snapshot()
        )

        with freeze_time(today):
            assert (generate_attendee_invoice("cs_test_123") is not None) is issued

    @pytest.mark.parametrize("country", ["BE", "PL"])
    def test_b2b_country_skips_domestic_business_buyer(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        country: str,
    ) -> None:
        _ready_in(organization, country, Organization.InvoicingMode.AUTO)
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            buyer_billing_snapshot=_business_snapshot(f"{country}0123456789"),
        )

        assert generate_attendee_invoice("cs_test_123") is None

    @pytest.mark.parametrize(
        ("country", "snapshot"),
        [
            ("BE", _consumer_snapshot()),
            ("BE", _business_snapshot("DE123456789")),
            ("PL", _consumer_snapshot()),
            ("PL", _business_snapshot("NL123456789B01")),
        ],
    )
    def test_b2b_country_still_invoices_consumers_and_foreign_businesses(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        country: str,
        snapshot: dict[str, t.Any],
    ) -> None:
        _ready_in(organization, country, Organization.InvoicingMode.AUTO)
        _create_payment(user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=snapshot)

        invoice = generate_attendee_invoice("cs_test_123")

        assert invoice is not None
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED

    def test_unrestricted_country_generates(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _ready_in(organization, "IT", Organization.InvoicingMode.AUTO)
        _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_consumer_snapshot()
        )

        assert generate_attendee_invoice("cs_test_123") is not None


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
class TestExistingDocuments:
    def test_pre_gate_draft_cannot_be_issued(
        self, _pdf: t.Any, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _ready_in(organization, "HR", Organization.InvoicingMode.HYBRID)
        draft = _create_draft(organization, event, member_user)
        draft.seller_vat_country = "HR"
        draft.save(update_fields=["seller_vat_country"])

        with pytest.raises(CountryComplianceError, match="Croatia"):
            issue_draft_invoice(draft)

        draft.refresh_from_db()
        assert draft.status == AttendeeInvoice.InvoiceStatus.DRAFT

    def test_pre_gate_issued_invoice_gets_no_credit_note(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _ready_in(organization, "GR", Organization.InvoicingMode.AUTO)
        payment = _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_consumer_snapshot()
        )
        invoice = _create_issued(organization, event, member_user)
        invoice.stripe_session_id = payment.stripe_session_id
        invoice.save(update_fields=["stripe_session_id"])

        assert generate_attendee_credit_note(payment.stripe_session_id, [payment.id]) is None
        assert not invoice.credit_notes.exists()
