"""Attendee credit notes follow their invoice's totals method and close its EN 16931 gap (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Refund, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_draft_service import issue_draft_invoice, update_draft_invoice
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
    net: str | None = None,
) -> Payment:
    amount = Decimal(gross)
    net_amount = Decimal(net) if net else (amount * 100 / (100 + Decimal(rate))).quantize(Decimal("0.01"))
    return _create_payment(
        user=user,
        event=event,
        tier=tier,
        session_id=session_id,
        amount=amount,
        net_amount=net_amount,
        vat_amount=amount - net_amount,
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
    assert sum(Decimal(li["net_amount"]) for li in cn.line_items) == cn.amount_net
    assert sum(Decimal(li["vat_amount"]) for li in cn.line_items) == cn.amount_vat


def _assert_credit_notes_sum_to_invoice(invoice: AttendeeInvoice, notes: list[AttendeeInvoiceCreditNote]) -> None:
    """Review fix 3: credited per ticket, the credit notes add back up to the invoice, column by column."""
    for cn in notes:
        _invariant(cn)
        assert cn.totals_method == "en16931"
    assert sum(cn.amount_gross for cn in notes) == invoice.total_gross
    assert sum(cn.amount_net for cn in notes) == invoice.total_net
    assert sum(cn.amount_vat for cn in notes) == invoice.total_vat
    assert sum(cn.rounding_amount for cn in notes) == invoice.rounding_amount
    invoice.refresh_from_db()
    assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED


class TestEn16931CreditNotes:
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

    def test_non_closing_partial_refund_uses_en16931_on_its_own_lines(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Review focus 4: a partial refund row on a 3-ticket invoice is its own balanced document."""
        payments = [_sale(member_user, event, event_ticket_tier, "cs_cn_en_2", guest=f"G{i}") for i in range(3)]
        invoice = generate_attendee_invoice("cs_cn_en_2")
        assert invoice is not None
        row = _refund(payments[1], "5.00")  # 5.00 at 13% is unreachable: 4.42 + 0.57 + 0.01
        cn = generate_attendee_credit_note("cs_cn_en_2", [payments[1].id], refund_ids=[row.id])
        assert cn is not None
        assert (cn.amount_gross, cn.amount_net, cn.amount_vat, cn.rounding_amount) == (
            Decimal("5.00"),
            Decimal("4.42"),
            Decimal("0.57"),
            Decimal("0.01"),
        )
        assert cn.totals_method == "en16931"
        _invariant(cn)
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED

    def test_three_tickets_refunded_one_at_a_time_sum_to_the_invoice(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Telescoping: 22.04 / 2.87 / -0.01, then 22.03 / 2.86 / +0.01, and the group closes at 22.04 / 2.86 / 0."""
        payments = [_sale(member_user, event, event_ticket_tier, "cs_cn_en_3", guest=f"G{i}") for i in range(3)]
        invoice = generate_attendee_invoice("cs_cn_en_3")
        assert invoice is not None
        assert (invoice.total_net, invoice.total_vat, invoice.rounding_amount) == (
            Decimal("66.11"),
            Decimal("8.59"),
            Decimal("0.00"),
        )
        notes = [generate_attendee_credit_note("cs_cn_en_3", [p.id]) for p in payments]
        assert all(notes)
        assert [(cn.amount_net, cn.amount_vat, cn.rounding_amount) for cn in notes if cn is not None] == [
            (Decimal("22.04"), Decimal("2.87"), Decimal("-0.01")),
            (Decimal("22.03"), Decimal("2.86"), Decimal("0.01")),
            (Decimal("22.04"), Decimal("2.86"), Decimal("0.00")),
        ]
        _assert_credit_notes_sum_to_invoice(invoice, [cn for cn in notes if cn is not None])

    @pytest.mark.parametrize("standard_rate_first", [False, True])
    def test_two_rate_cart_sums_to_the_invoice_in_either_refund_order(
        self,
        auto_org: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        standard_rate_first: bool,
    ) -> None:
        """Fix A: each (category, rate) group closes on its own, whatever order the tickets are refunded in."""
        session = f"cs_cn_en_mix_{int(standard_rate_first)}"
        payments = [_sale(member_user, event, event_ticket_tier, session, guest=f"G{i}") for i in range(3)]
        payments.append(_sale(member_user, event, event_ticket_tier, session, "10.00", "22.00", guest="G22"))
        invoice = generate_attendee_invoice(session)
        assert invoice is not None
        assert invoice.has_mixed_vat_rates
        order = payments[::-1] if standard_rate_first else payments
        notes = [generate_attendee_credit_note(session, [p.id]) for p in order]
        _assert_credit_notes_sum_to_invoice(invoice, [cn for cn in notes if cn is not None])

    def test_fifty_tickets_refunded_per_ticket_sum_to_the_invoice(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        payments = [_sale(member_user, event, event_ticket_tier, "cs_cn_en_50", guest=f"G{i}") for i in range(50)]
        invoice = generate_attendee_invoice("cs_cn_en_50")
        assert invoice is not None
        notes = [generate_attendee_credit_note("cs_cn_en_50", [p.id]) for p in payments]
        _assert_credit_notes_sum_to_invoice(invoice, [cn for cn in notes if cn is not None])

    def test_ten_tickets_refunded_by_refund_rows_sum_to_the_invoice(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        payments = [
            _sale(member_user, event, event_ticket_tier, "cs_cn_en_10", "10.00", "22.00", guest=f"G{i}")
            for i in range(10)
        ]
        invoice = generate_attendee_invoice("cs_cn_en_10")
        assert invoice is not None
        notes = [
            generate_attendee_credit_note("cs_cn_en_10", [p.id], refund_ids=[_refund(p, "10.00").id]) for p in payments
        ]
        _assert_credit_notes_sum_to_invoice(invoice, [cn for cn in notes if cn is not None])


class TestEditedDrafts:
    """Fixes B and C: a DRAFT edited before issuing still gets a sound credit note."""

    @staticmethod
    def _hybrid(org: Organization) -> None:
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()

    def test_rate_edited_draft_refund_reverses_the_edited_rate(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        self._hybrid(auto_org)
        payment = _sale(member_user, event, event_ticket_tier, "cs_cn_en_edit", "24.90", "20.00")
        invoice = generate_attendee_invoice("cs_cn_en_edit")
        assert invoice is not None
        edited = {**invoice.line_items[0], "vat_rate": "10.00"}
        invoice = issue_draft_invoice(update_draft_invoice(invoice, {"line_items": [edited]}))
        assert invoice.vat_breakdown[0]["vat_rate"] == Decimal("10.00")

        cn = generate_attendee_credit_note("cs_cn_en_edit", [payment.id])
        assert cn is not None
        assert cn.totals_method == "en16931"
        # The note copies the invoice's edited 10% line (#1250), not the payment's 20%, and so
        # lands exactly on the invoice's own figures.
        assert [li["vat_rate"] for li in cn.line_items] == ["10.00"]
        assert (cn.amount_net, cn.amount_vat) == (invoice.total_net, invoice.total_vat)
        assert cn.rounding_amount == invoice.rounding_amount
        _invariant(cn)

    def test_reverse_charge_draft_refund_builds_zero_rate_lines(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        self._hybrid(auto_org)
        payment = _sale(member_user, event, event_ticket_tier, "cs_cn_en_rc", "24.90", "20.00")
        invoice = generate_attendee_invoice("cs_cn_en_rc")
        assert invoice is not None
        zero = {**invoice.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "net_amount": "24.90"}
        invoice = issue_draft_invoice(update_draft_invoice(invoice, {"reverse_charge": True, "line_items": [zero]}))
        assert invoice.reverse_charge and invoice.total_vat == 0

        cn = generate_attendee_credit_note("cs_cn_en_rc", [payment.id])
        assert cn is not None
        assert [(li["vat_category"], li["vat_rate"], li["vat_amount"], li["net_amount"]) for li in cn.line_items] == [
            ("AE", "0.00", "0.00", "24.90")
        ]
        assert (cn.amount_gross, cn.amount_net, cn.amount_vat, cn.rounding_amount) == (
            Decimal("24.90"),
            Decimal("24.90"),
            Decimal("0.00"),
            Decimal("0.00"),
        )
        assert cn.totals_method == "en16931"
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED


class TestLegacyInvoiceCreditNotes:
    """Review fix 2: a credit note on a legacy invoice keeps main's arithmetic and is stamped legacy."""

    def test_full_refund_of_legacy_invoice_mirrors_its_stored_figures(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        invoice = _create_issued(auto_org, event, member_user)
        assert invoice.totals_method == "legacy"
        payment = _sale(member_user, event, event_ticket_tier, invoice.stripe_session_id, "24.90", "13.00", net="22.04")
        assert payment.vat_amount == Decimal("2.86")
        cn = generate_attendee_credit_note(invoice.stripe_session_id, [payment.id])
        assert cn is not None
        assert (cn.amount_gross, cn.amount_net, cn.amount_vat, cn.rounding_amount) == (
            Decimal("24.90"),
            Decimal("22.04"),
            Decimal("2.86"),
            Decimal("0.00"),
        )
        assert cn.totals_method == "legacy"
        assert (cn.line_items[0]["net_amount"], cn.line_items[0]["vat_amount"]) == ("22.04", "2.86")

    def test_partial_refund_of_legacy_invoice_pro_rates_the_checkout_vat(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        invoice = _create_issued(auto_org, event, member_user)
        payment = _sale(member_user, event, event_ticket_tier, invoice.stripe_session_id, "40.00", "22.00", net="33.33")
        row = _refund(payment, "10.00")
        cn = generate_attendee_credit_note(invoice.stripe_session_id, [payment.id], refund_ids=[row.id])
        assert cn is not None
        # main: VAT = 10.00 * 6.67 / 40.00 = 1.6675 -> 1.67, net = 10.00 - 1.67
        assert (cn.amount_gross, cn.amount_net, cn.amount_vat, cn.rounding_amount) == (
            Decimal("10.00"),
            Decimal("8.33"),
            Decimal("1.67"),
            Decimal("0.00"),
        )
        assert cn.totals_method == "legacy"


class TestSkippedPath:
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
