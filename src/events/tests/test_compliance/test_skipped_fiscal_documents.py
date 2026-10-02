"""Recording the invoices and credit notes a country policy made Revel skip (#1091)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from accounts.models import RevelUser
from events.compliance import BuyerContext, get_policy_for_country
from events.compliance.base import ALL_NEXUS
from events.models import Event, Organization, Payment, Refund, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_issued,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]

Kind = SkippedFiscalDocument.Kind
Reason = SkippedFiscalDocument.ReasonCode


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"):
        yield


def _ready_in(org: Organization, country: str) -> Organization:
    _make_org_invoicing_ready(org)
    org.vat_country_code = country
    org.vat_id = f"{country}123456789"
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return org


def _business(vat_id: str, status: str = "valid") -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": vat_id, "vat_country_code": vat_id[:2], "vat_id_status": status}


def _consumer() -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": "", "vat_country_code": "", "billing_name": "Jane Doe"}


@pytest.fixture
def sale(event: Event, event_ticket_tier: TicketTier, member_user: RevelUser) -> t.Callable[..., Payment]:
    """A succeeded payment on ``cs_test_123`` with the given billing snapshot."""

    def make(snapshot: dict[str, t.Any], **kwargs: t.Any) -> Payment:
        return _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=snapshot, **kwargs
        )

    return make


class TestDecisionCodes:
    def test_reason_codes_match_the_policy_decisions(self) -> None:
        """The model's reason codes are the strings the mixins put on ``Decision.code``."""
        b2b = get_policy_for_country("BE").attendee_invoicing(BuyerContext(vat_country="BE"), ALL_NEXUS)
        fiscal = get_policy_for_country("HR").attendee_invoicing(BuyerContext(), ALL_NEXUS)

        assert (b2b.code, b2b.country) == (Reason.B2B_E_INVOICING, "BE")
        assert (fiscal.code, fiscal.country) == (Reason.FISCALIZED_INVOICING, "HR")


class TestInvoiceSkips:
    def test_be_domestic_business_buyer_is_recorded(
        self, organization: Organization, event: Event, member_user: RevelUser, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        payment = sale(_business("BE0123456789"))

        assert generate_attendee_invoice("cs_test_123") is None

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.kind, doc.reason_code, doc.policy_country) == (Kind.INVOICE, Reason.B2B_E_INVOICING, "BE")
        assert "Peppol" in doc.reason
        assert (doc.organization, doc.event, doc.user) == (organization, event, member_user)
        assert doc.stripe_session_id == "cs_test_123"
        assert list(doc.payments.all()) == [payment]
        assert (doc.buyer_name, doc.buyer_email) == ("Buyer GmbH", "buyer@example.de")
        assert (doc.buyer_vat_id, doc.buyer_vat_country, doc.buyer_vat_id_status) == ("BE0123456789", "BE", "valid")
        assert (doc.total_gross, doc.total_net, doc.total_vat) == (
            Decimal("100.00"),
            Decimal("81.97"),
            Decimal("18.03"),
        )
        assert doc.currency == "EUR"
        assert doc.vat_breakdown[0]["vat_rate"] == Decimal("22.00")
        assert len(doc.line_items) == 1
        assert doc.notified_at is None and doc.resolved_at is None

    def test_be_foreign_business_buyer_is_invoiced_not_recorded(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        sale(_business("DE123456789"))

        assert generate_attendee_invoice("cs_test_123") is not None
        assert not SkippedFiscalDocument.objects.exists()

    def test_pl_foreign_business_buyer_is_recorded(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "PL")
        sale(_business("NL123456789B01", status="unavailable"))

        assert generate_attendee_invoice("cs_test_123") is None

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.reason_code, doc.policy_country) == (Reason.B2B_E_INVOICING, "PL")
        assert "KSeF" in doc.reason
        assert doc.buyer_vat_id_status == "unavailable"

    def test_vies_rejected_id_is_a_consumer_and_invoiced(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "PL")
        sale(_business("PL0123456789", status="invalid"))

        assert generate_attendee_invoice("cs_test_123") is not None
        assert not SkippedFiscalDocument.objects.exists()

    @pytest.mark.parametrize(("today", "skipped"), [("2026-12-31 12:00", False), ("2027-01-01 12:00", True)])
    def test_spain_consumers_skipped_from_2027(
        self, organization: Organization, sale: t.Callable[..., Payment], today: str, skipped: bool
    ) -> None:
        _ready_in(organization, "ES")
        sale(_consumer())

        with freeze_time(today):
            generate_attendee_invoice("cs_test_123")

        assert SkippedFiscalDocument.objects.exists() is skipped
        if skipped:
            doc = SkippedFiscalDocument.objects.get()
            assert (doc.reason_code, doc.policy_country) == (Reason.FISCALIZED_INVOICING, "ES")
            assert doc.buyer_vat_id == ""

    def test_venue_country_policy_is_recorded(
        self, organization: Organization, event: Event, sale: t.Callable[..., Payment]
    ) -> None:
        """An AT org's event held in Hungary: the refusing country is the venue's."""
        _ready_in(organization, "AT")
        event.vat_country_code = "HU"
        event.save(update_fields=["vat_country_code"])
        sale(_consumer())

        assert generate_attendee_invoice("cs_test_123") is None
        assert SkippedFiscalDocument.objects.get().policy_country == "HU"

    def test_retry_is_idempotent_and_stays_skipped(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"))

        assert generate_attendee_invoice("cs_test_123") is None
        # Even if the org moves country (the policy would now allow it), a decided session stays skipped.
        organization.vat_country_code = "IT"
        organization.save(update_fields=["vat_country_code"])
        assert generate_attendee_invoice("cs_test_123") is None

        assert SkippedFiscalDocument.objects.count() == 1
        assert not AttendeeInvoice.objects.exists()

    def test_multi_ticket_cart_records_one_document(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        snapshot = _business("BE0123456789")
        sale(snapshot)
        sale(snapshot, amount=Decimal("50.00"), net_amount=Decimal("45.45"), vat_amount=Decimal("4.55"))

        generate_attendee_invoice("cs_test_123")

        doc = SkippedFiscalDocument.objects.get()
        assert doc.payments.count() == 2
        assert doc.total_gross == Decimal("150.00")
        assert doc.total_vat == Decimal("22.58")


def _refund(payment: Payment, amount: str) -> Refund:
    return Refund.objects.create(
        payment=payment,
        amount=Decimal(amount),
        currency="EUR",
        status=Refund.RefundStatus.SUCCEEDED,
        source=Refund.Source.ORGANIZER_API,
    )


class TestCreditNoteSkips:
    def test_refund_on_a_skipped_invoice_records_a_skipped_credit_note(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        payment = sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        parent = SkippedFiscalDocument.objects.get(kind=Kind.INVOICE)
        row = _refund(payment, "40.00")

        assert generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[row.id]) is None

        doc = SkippedFiscalDocument.objects.get(kind=Kind.CREDIT_NOTE)
        assert doc.parent == parent and doc.invoice is None
        assert (doc.reason_code, doc.policy_country, doc.reason) == (
            parent.reason_code,
            parent.policy_country,
            parent.reason,
        )
        assert list(doc.refunds.all()) == [row]
        assert list(doc.payments.all()) == [payment]
        assert doc.total_gross == Decimal("40.00")
        assert doc.total_vat == Decimal("7.21")  # pro rata: 40 * 18.03 / 100
        assert (doc.buyer_vat_id, doc.buyer_vat_id_status) == ("BE0123456789", "valid")

    def test_credit_note_skip_is_idempotent_on_exact_and_superset_retries(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        payment = sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        first = _refund(payment, "40.00")

        generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[first.id])
        generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[first.id])
        assert SkippedFiscalDocument.objects.filter(kind=Kind.CREDIT_NOTE).count() == 1

        # A second partial refund, then a duplicate-webhook retry carrying both rows.
        second = _refund(payment, "60.00")
        generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[first.id, second.id])
        generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[first.id, second.id])

        docs = SkippedFiscalDocument.objects.filter(kind=Kind.CREDIT_NOTE).order_by("total_gross")
        assert [d.total_gross for d in docs] == [Decimal("40.00"), Decimal("60.00")]
        assert list(docs[1].refunds.all()) == [second]

    def test_legacy_payment_keyed_retry_is_idempotent(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "PL")
        payment = sale(_business("PL0123456789"))
        generate_attendee_invoice("cs_test_123")

        generate_attendee_credit_note("cs_test_123", [payment.id])
        generate_attendee_credit_note("cs_test_123", [payment.id])

        doc = SkippedFiscalDocument.objects.get(kind=Kind.CREDIT_NOTE)
        assert doc.total_gross == Decimal("100.00")
        assert not doc.refunds.exists()

    @pytest.mark.parametrize("country", ["HR", "PT", "RO", "SI", "GR", "HU"])
    def test_pre_gate_invoice_in_a_fiscalized_country_records_the_credit_note(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        sale: t.Callable[..., Payment],
        country: str,
    ) -> None:
        _ready_in(organization, country)
        payment = sale(_consumer())
        invoice = _create_issued(organization, event, member_user)
        invoice.stripe_session_id = payment.stripe_session_id
        invoice.seller_vat_country = country
        invoice.save(update_fields=["stripe_session_id", "seller_vat_country"])
        row = _refund(payment, "100.00")

        assert generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[row.id]) is None
        generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[row.id])

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.kind, doc.reason_code, doc.policy_country) == (
            Kind.CREDIT_NOTE,
            Reason.FISCALIZED_INVOICING,
            country,
        )
        assert doc.invoice == invoice and doc.parent is None
        assert (doc.buyer_name, doc.total_gross) == (invoice.buyer_name, Decimal("100.00"))
        assert not invoice.credit_notes.exists()

    def test_spain_invoice_refunded_after_the_flip_records_the_credit_note(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "ES")
        payment = sale(_consumer())
        with freeze_time("2026-11-15"):
            invoice = generate_attendee_invoice("cs_test_123")
        assert invoice is not None
        row = _refund(payment, "100.00")

        with freeze_time("2027-02-01"):
            assert generate_attendee_credit_note("cs_test_123", [payment.id], refund_ids=[row.id]) is None

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.kind, doc.policy_country, doc.invoice) == (Kind.CREDIT_NOTE, "ES", invoice)

    @pytest.mark.parametrize("country", ["BE", "PL"])
    def test_pre_gate_b2b_invoice_records_the_credit_note(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        sale: t.Callable[..., Payment],
        country: str,
    ) -> None:
        """An invoice issued to a domestic business before the gate gets no Revel credit note."""
        _ready_in(organization, country)
        payment = sale(_business(f"{country}0123456789"))
        invoice = _create_issued(organization, event, member_user)
        invoice.stripe_session_id = payment.stripe_session_id
        invoice.buyer_vat_id = f"{country}0123456789"
        invoice.save(update_fields=["stripe_session_id", "buyer_vat_id"])

        generate_attendee_credit_note("cs_test_123", [payment.id])

        doc = SkippedFiscalDocument.objects.get()
        assert (doc.kind, doc.reason_code, doc.invoice) == (Kind.CREDIT_NOTE, Reason.B2B_E_INVOICING, invoice)

    def test_refund_on_a_sale_without_invoice_or_skip_records_nothing(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "IT")
        payment = sale(_consumer())

        assert generate_attendee_credit_note("cs_test_123", [payment.id]) is None
        assert not SkippedFiscalDocument.objects.exists()
