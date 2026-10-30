"""Invoice lines snapshot the payment and tier they charge; edits and credit notes follow them (#1242, #1250)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

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

_IDS = ("line_id", "payment_id", "tier_id")


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
        assert UUID(invoice.line_items[0]["line_id"])


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

    def test_unknown_line_id_gives_no_provenance(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        invoice, t1 = self._draft(member_user, event)
        first, second = invoice.line_items
        forged = {**_zero(second), "line_id": str(uuid4()), "tier_id": str(t1.id)}
        invoice = update_draft_invoice(invoice, {"line_items": [first, forged]})
        assert not {"line_id", "payment_id", "tier_id"} & invoice.line_items[1].keys()
        assert invoice.vat_exemptions["E"]["text"] == "Org default reason"

    def test_client_echoing_no_line_id_gives_no_provenance(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, _t1 = self._draft(member_user, event)
        invoice = update_draft_invoice(invoice, {"line_items": [_without_ids(_zero(li)) for li in invoice.line_items]})
        assert not any(k in li for li in invoice.line_items for k in ("line_id", "payment_id", "tier_id"))
        # The invoice had ids before the edit, so it never drops to the legacy path: org default only.
        assert invoice.vat_exemptions["E"]["text"] == "Org default reason"

    def test_legacy_lines_keep_the_stored_text(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        invoice, _t1 = self._draft(member_user, event)
        legacy = [_without_ids(li) for li in invoice.line_items]
        invoice.line_items = legacy
        invoice.vat_exemptions = {"E": {"text": "Stored legacy text", "code": ""}}
        invoice.save()
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(li) for li in legacy]})
        assert invoice.vat_exemptions["E"]["text"] == "Stored legacy text"


class TestProvenanceMatching:
    """Provenance rides on the server-issued ``line_id``: never position, content or a client-stated payment."""

    def _two(self, user: RevelUser, event: Event, gross_b: str = "50.00") -> tuple[AttendeeInvoice, Payment, Payment]:
        t1, t2 = _tier(event, "T1", "R1"), _tier(event, "T2", "R2")
        pa = _pay(user, event, t1, "cs_prov_m", "A", "24.90")
        pb = _pay(user, event, t2, "cs_prov_m", "B", gross_b)
        invoice = generate_attendee_invoice("cs_prov_m")
        assert invoice is not None
        return invoice, pa, pb

    @staticmethod
    def _by_gross(invoice: AttendeeInvoice) -> dict[str, str | None]:
        return {li["unit_price_gross"]: li.get("payment_id") for li in invoice.line_items}

    def test_reorder_with_echoed_line_ids_keeps_each_lines_own_ids(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, pa, pb = self._two(member_user, event)
        a, b = invoice.line_items
        invoice = update_draft_invoice(invoice, {"line_items": [b, a]})
        assert self._by_gross(invoice) == {"24.90": str(pa.id), "50.00": str(pb.id)}

    def test_reorder_of_identical_lines_keeps_per_payment_rates(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, pa, pb = self._two(member_user, event, gross_b="24.90")
        a, b = invoice.line_items
        a = {**a, "description": "Same", "vat_rate": "10.00"}
        b = {**b, "description": "Same", "vat_rate": "20.00"}
        invoice = update_draft_invoice(invoice, {"line_items": [a, b]})
        invoice = update_draft_invoice(invoice, {"line_items": [invoice.line_items[1], invoice.line_items[0]]})
        assert [(li["payment_id"], li["vat_rate"]) for li in invoice.line_items] == [
            (str(pb.id), "20.00"),
            (str(pa.id), "10.00"),
        ]

    def test_editing_description_and_rate_with_the_line_id_keeps_provenance_and_credit_reverses_it(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, pa, _pb = self._two(member_user, event)
        a, b = invoice.line_items
        edited = {**a, "description": "Totally different", "unit_price_gross": "24.90", "vat_rate": "10.00"}
        invoice = issue_draft_invoice(update_draft_invoice(invoice, {"line_items": [edited, b]}))
        assert invoice.line_items[0]["payment_id"] == str(pa.id)
        note = generate_attendee_credit_note("cs_prov_m", [pa.id])
        assert note is not None
        assert [li["vat_rate"] for li in note.line_items] == ["10.00"]

    def test_a_client_cannot_state_a_payment_or_tier(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, pa, pb = self._two(member_user, event)
        a, b = invoice.line_items
        # A's line_id with B's payment/tier: this is an edit of line A, whatever else it claims.
        moved = {**a, "payment_id": b["payment_id"], "tier_id": b["tier_id"], "description": "B's content"}
        invoice = update_draft_invoice(invoice, {"line_items": [moved, b]})
        assert invoice.line_items[0]["payment_id"] == str(pa.id)
        assert invoice.line_items[0]["tier_id"] == a["tier_id"]
        assert invoice.line_items[1]["payment_id"] == str(pb.id)

    def test_duplicate_line_id_is_honoured_once(self, org: Organization, event: Event, member_user: RevelUser) -> None:
        invoice, pa, _pb = self._two(member_user, event)
        a, _b = invoice.line_items
        invoice = update_draft_invoice(invoice, {"line_items": [a, {**a, "description": "copy"}]})
        assert invoice.line_items[0]["payment_id"] == str(pa.id)
        assert not {"line_id", "payment_id", "tier_id"} & invoice.line_items[1].keys()

    def test_delete_and_add_gives_the_new_line_nothing(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, _pa, pb = self._two(member_user, event)
        _a, b = invoice.line_items
        hand_made = {**_without_ids(b), "description": "Hand made", "unit_price_gross": "7.00"}
        invoice = update_draft_invoice(invoice, {"line_items": [hand_made, b]})
        assert self._by_gross(invoice) == {"7.00": None, "50.00": str(pb.id)}

    def test_removing_a_line_keeps_the_rest_off_the_legacy_path(
        self, org: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice, _pa, _pb = self._two(member_user, event)
        a, b = invoice.line_items
        invoice = update_draft_invoice(invoice, {"line_items": [_zero(a), _zero(b)]})
        invoice = update_draft_invoice(invoice, {"line_items": [invoice.line_items[0]]})
        assert invoice.vat_exemptions["E"]["text"] == "R1"
        # An invoice that once had ids never drops to the legacy path, even if every line is replaced.
        replaced = {**_without_ids(_zero(a)), "description": "new"}
        invoice = update_draft_invoice(invoice, {"line_items": [replaced]})
        assert invoice.vat_exemptions["E"]["text"] == "Org default reason"


class TestCumulativeProration:
    """Review: partial refunds of a payment sum to exactly the invoice line."""

    @pytest.mark.parametrize(("line_gross", "paid", "refund"), [("20.00", "24.90", "8.30"), ("10.00", "3.00", "1.00")])
    def test_three_refunds_sum_to_the_line_and_cancel(
        self, org: Organization, event: Event, member_user: RevelUser, line_gross: str, paid: str, refund: str
    ) -> None:
        session = f"cs_prov_pro_{paid}"
        payment = _pay(member_user, event, _tier(event, "T"), session, "A", paid)
        invoice = generate_attendee_invoice(session)
        assert invoice is not None
        edited = {**invoice.line_items[0], "unit_price_gross": line_gross}
        invoice = issue_draft_invoice(update_draft_invoice(invoice, {"line_items": [edited]}))
        notes = [
            generate_attendee_credit_note(session, [payment.id], refund_ids=[_refund(payment, refund).id])
            for _ in range(3)
        ]
        assert all(notes)
        grosses = [Decimal(li["unit_price_gross"]) for n in notes if n for li in n.line_items]
        assert sum(grosses) == Decimal(line_gross)
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED


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
