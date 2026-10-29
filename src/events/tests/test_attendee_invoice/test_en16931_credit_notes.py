"""Attendee credit notes carry EN 16931 totals and their own BT-114 rounding (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Refund, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_issued,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"), patch(MOCK_SEND_EMAIL):
        yield


@pytest.fixture
def auto_org(organization: Organization) -> Organization:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return org


def _sale(
    user: RevelUser,
    event: Event,
    tier: TicketTier,
    session_id: str,
    gross: str = "24.90",
    rate: str = "13.00",
    guest: str = "Guest",
    snapshot: dict[str, t.Any] | None = None,
) -> Payment:
    amount = Decimal(gross)
    net = (amount * 100 / (100 + Decimal(rate))).quantize(Decimal("0.01"))
    return _create_payment(
        user=user,
        event=event,
        tier=tier,
        session_id=session_id,
        amount=amount,
        net_amount=net,
        vat_amount=amount - net,
        vat_rate=Decimal(rate),
        buyer_billing_snapshot=snapshot or _default_billing_snapshot(),
        guest_name=guest,
    )


def _refund(payment: Payment, amount: str) -> Refund:
    return Refund.objects.create(
        payment=payment,
        amount=Decimal(amount),
        currency="EUR",
        status=Refund.RefundStatus.SUCCEEDED,
        source=Refund.Source.ORGANIZER_API,
    )


def _invariant(cn: AttendeeInvoiceCreditNote) -> None:
    assert cn.amount_net + cn.amount_vat + cn.rounding_amount == cn.amount_gross


class TestCreditNoteTotals:
    def test_full_refund_credit_note_mirrors_invoice_totals(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        payment = _sale(member_user, event, event_ticket_tier, "cs_cn_en_1")
        invoice = generate_attendee_invoice("cs_cn_en_1")
        assert invoice is not None
        cn = generate_attendee_credit_note("cs_cn_en_1", [payment.id])
        assert cn is not None
        assert (cn.amount_gross, cn.amount_net, cn.amount_vat, cn.rounding_amount) == (
            Decimal("24.90"),
            Decimal("22.04"),
            Decimal("2.87"),
            Decimal("-0.01"),
        )
        assert cn.totals_method == "en16931"
        assert cn.line_items[0]["net_amount"] == "22.04"
        assert cn.line_items[0]["vat_amount"] == "2.87"
        _invariant(cn)

    def test_partial_refund_credit_note_invariant(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Review focus 4: a partial refund row on a 3-ticket invoice is its own balanced document."""
        payments = [_sale(member_user, event, event_ticket_tier, "cs_cn_en_2", guest=f"G{i}") for i in range(3)]
        invoice = generate_attendee_invoice("cs_cn_en_2")
        assert invoice is not None
        row = _refund(payments[1], "5.00")  # 5.00 at 13% is unreachable: 4.42 + 0.57 + 0.01
        cn = generate_attendee_credit_note("cs_cn_en_2", [payments[1].id], refund_ids=[row.id])
        assert cn is not None
        assert cn.amount_gross == Decimal("5.00")
        assert cn.rounding_amount == Decimal("0.01")
        assert cn.totals_method == "en16931"
        _invariant(cn)
        assert sum(Decimal(li["net_amount"]) for li in cn.line_items) == cn.amount_net
        invoice.refresh_from_db()
        assert invoice.status == invoice.InvoiceStatus.ISSUED

    def test_credit_note_for_legacy_invoice_is_en16931(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """A credit note is a new document: it uses the EN 16931 method even when the invoice is legacy."""
        invoice = _create_issued(auto_org, event, member_user)
        assert invoice.totals_method == "legacy"
        payment = _sale(member_user, event, event_ticket_tier, invoice.stripe_session_id, "100.00", "22.00")
        cn = generate_attendee_credit_note(invoice.stripe_session_id, [payment.id])
        assert cn is not None
        assert cn.totals_method == "en16931"
        assert (cn.amount_net, cn.amount_vat, cn.rounding_amount) == (
            Decimal("81.97"),
            Decimal("18.03"),
            Decimal("0.00"),
        )
        _invariant(cn)

    def test_skipped_credit_note_path_unchanged(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """A refund on a skipped sale still records a skipped credit note with its totals populated."""
        auto_org.vat_country_code = "BE"
        auto_org.vat_id = "BE123456789"
        auto_org.save()
        snapshot = {**_default_billing_snapshot(), "vat_id": "BE0123456789", "vat_country_code": "BE"}
        snapshot["vat_id_status"] = "valid"
        payment = _sale(member_user, event, event_ticket_tier, "cs_cn_en_skip", "40.00", "22.00", snapshot=snapshot)
        assert generate_attendee_invoice("cs_cn_en_skip") is None
        row = _refund(payment, "40.00")
        assert generate_attendee_credit_note("cs_cn_en_skip", [payment.id], refund_ids=[row.id]) is None
        doc = SkippedFiscalDocument.objects.get(kind=SkippedFiscalDocument.Kind.CREDIT_NOTE)
        assert doc.total_gross == Decimal("40.00")
        assert doc.total_net > 0 and doc.total_vat > 0
        assert doc.total_net + doc.total_vat == doc.total_gross
        assert len(doc.line_items) == 1
        assert list(doc.refunds.all()) == [row]
        assert not AttendeeInvoiceCreditNote.objects.exists()
