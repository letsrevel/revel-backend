"""Invoice lines snapshot the payment and tier they charge; edits and credit notes follow them (#1242, #1250)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Refund, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_draft_service import issue_draft_invoice, update_draft_invoice
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]

_IDS = ("payment_id", "tier_id")


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"), patch(MOCK_SEND_EMAIL):
        yield


@pytest.fixture
def org(organization: Organization) -> Organization:
    organization = _make_org_invoicing_ready(organization)
    organization.invoicing_mode = Organization.InvoicingMode.HYBRID
    organization.vat_exemption_reason = "Org default reason"
    organization.save()
    return organization


def _tier(event: Event, name: str, reason: str = "") -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("24.90"),
        vat_rate=Decimal("20.00"),
        vat_exemption_reason=reason,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


def _pay(user: RevelUser, event: Event, tier: TicketTier, session: str, guest: str, gross: str = "24.90") -> Payment:
    amount = Decimal(gross)
    net = (amount * 100 / 120).quantize(Decimal("0.01"))
    return _create_payment(
        user=user,
        event=event,
        tier=tier,
        session_id=session,
        amount=amount,
        net_amount=net,
        vat_amount=amount - net,
        vat_rate=Decimal("20.00"),
        buyer_billing_snapshot=_default_billing_snapshot(),
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


def _zero(line: dict[str, t.Any]) -> dict[str, t.Any]:
    return {**line, "vat_rate": "0.00", "vat_amount": "0.00", "net_amount": line["unit_price_gross"]}


def _without_ids(line: dict[str, t.Any]) -> dict[str, t.Any]:
    return {k: v for k, v in line.items() if k not in _IDS}


class TestGenerationSnapshot:
    def test_lines_carry_payment_and_tier_ids(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        tier = _tier(event, "T1")
        payment = _pay(member_user, event, tier, "cs_prov_1", "A")
        invoice = generate_attendee_invoice("cs_prov_1")
        assert invoice is not None
        assert invoice.line_items[0]["payment_id"] == str(payment.id)
        assert invoice.line_items[0]["tier_id"] == str(tier.id)


class TestDraftExemptionFromTier:
    """#1242: an E line's reason is resolved from its own tier on every edit."""

    def _draft(self, user: RevelUser, event: Event) -> tuple[AttendeeInvoice, TicketTier]:
        t1, t2 = _tier(event, "T1", "Art. 132 tier one"), _tier(event, "T2", "")
        _pay(user, event, t1, "cs_prov_ex", "A")
        _pay(user, event, t2, "cs_prov_ex", "B")
        invoice = generate_attendee_invoice("cs_prov_ex")
        assert invoice is not None
        return invoice, t1

    def test_zeroed_line_takes_its_tier_reason_and_edits_track_tier_changes(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, t1 = self._draft(member_user, event)
        first, second = invoice.line_items
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(first), second]})
        assert invoice.vat_exemptions["E"]["text"] == "Art. 132 tier one"

        t1.vat_exemption_reason = "Art. 135 corrected"
        t1.save()
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(first), second]})
        assert invoice.vat_exemptions["E"]["text"] == "Art. 135 corrected"

    def test_tier_without_reason_falls_back_to_org_default_and_removed_lines_drop_their_text(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, _t1 = self._draft(member_user, event)
        first, second = invoice.line_items
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(first), _zero(second)]})
        assert invoice.vat_exemptions["E"]["text"] == "Art. 132 tier one; Org default reason"
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(second)]})
        assert invoice.vat_exemptions["E"]["text"] == "Org default reason"

    def test_forged_or_foreign_ids_are_ignored(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        invoice, t1 = self._draft(member_user, event)
        first, second = invoice.line_items
        forged = {**_zero(second), "payment_id": "00000000-0000-0000-0000-000000000001", "tier_id": str(t1.id)}
        invoice = update_draft_invoice(invoice, {"line_items": [first, forged]})
        assert "payment_id" not in invoice.line_items[1]
        assert "tier_id" not in invoice.line_items[1]
        assert invoice.vat_exemptions["E"]["text"] == "Org default reason"

    def test_client_echoing_no_ids_keeps_provenance_by_position(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, _t1 = self._draft(member_user, event)
        before = [li["payment_id"] for li in invoice.line_items]
        invoice = update_draft_invoice(invoice, {"line_items": [_without_ids(_zero(li)) for li in invoice.line_items]})
        assert [li["payment_id"] for li in invoice.line_items] == before
        assert invoice.vat_exemptions["E"]["text"] == "Art. 132 tier one; Org default reason"

    def test_legacy_lines_keep_the_stored_text(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        invoice, _t1 = self._draft(member_user, event)
        legacy = [_without_ids(li) for li in invoice.line_items]
        invoice.line_items = legacy
        invoice.vat_exemptions = {"E": {"text": "Stored legacy text", "code": ""}}
        invoice.save()
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(li) for li in legacy]})
        assert invoice.vat_exemptions["E"]["text"] == "Stored legacy text"


class TestCreditNotesFollowEditedLines:
    """#1250: credit notes reverse the invoice's own (edited) lines."""

    def _edited(
        self, user: RevelUser, event: Event, session: str, rate: str = "10.00"
    ) -> tuple[AttendeeInvoice, Payment]:
        payment = _pay(user, event, _tier(event, "T"), session, "A", "100.00")
        invoice = generate_attendee_invoice(session)
        assert invoice is not None
        edited = {**invoice.line_items[0], "vat_rate": rate, "description": "Edited description"}
        invoice = issue_draft_invoice(update_draft_invoice(invoice, {"line_items": [edited]}))
        return invoice, payment

    def test_partial_then_full_credit_reverse_the_edited_rate(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, payment = self._edited(member_user, event, "cs_prov_cn1")
        first = generate_attendee_credit_note("cs_prov_cn1", [payment.id], refund_ids=[_refund(payment, "40.00").id])
        second = generate_attendee_credit_note("cs_prov_cn1", [payment.id], refund_ids=[_refund(payment, "60.00").id])
        assert first is not None and second is not None
        for note in (first, second):
            assert [li["vat_rate"] for li in note.line_items] == ["10.00"]
            assert note.line_items[0]["description"].endswith("Edited description")
        assert first.line_items[0]["description"].startswith("Partial refund")
        assert first.amount_gross + second.amount_gross == invoice.total_gross
        assert first.amount_net + second.amount_net == invoice.total_net
        assert first.amount_vat + second.amount_vat == invoice.total_vat
        assert first.rounding_amount + second.rounding_amount == invoice.rounding_amount
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED

    def test_payment_keyed_full_credit_copies_the_line(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, payment = self._edited(member_user, event, "cs_prov_cn2")
        note = generate_attendee_credit_note("cs_prov_cn2", [payment.id])
        assert note is not None
        assert [li["vat_rate"] for li in note.line_items] == ["10.00"]
        assert (note.amount_net, note.amount_vat) == (invoice.total_net, invoice.total_vat)

    def test_edited_to_zero_rate_is_credited_exempt(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _invoice, payment = self._edited(member_user, event, "cs_prov_cn3", rate="0.00")
        note = generate_attendee_credit_note("cs_prov_cn3", [payment.id])
        assert note is not None
        assert [(li["vat_rate"], li["vat_category"]) for li in note.line_items] == [("0.00", "E")]
        assert note.amount_vat == 0

    def test_legacy_lines_without_ids_fall_back_to_the_payment(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        payment = _pay(member_user, event, _tier(event, "T"), "cs_prov_cn4", "A", "100.00")
        invoice = generate_attendee_invoice("cs_prov_cn4")
        assert invoice is not None
        invoice.line_items = [_without_ids(li) for li in invoice.line_items]
        invoice.status = AttendeeInvoice.InvoiceStatus.ISSUED
        invoice.save()
        note = generate_attendee_credit_note("cs_prov_cn4", [payment.id])
        assert note is not None
        assert [li["vat_rate"] for li in note.line_items] == ["20.00"]
