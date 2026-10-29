"""Eligibility and EN 16931 mapping of attendee invoices and credit notes (#1235)."""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from euinvoice import calc
from euinvoice.model import Invoice

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, TotalsMethod
from events.service import einvoice_mapping as mapping
from events.tests.test_einvoice._fixtures import CARD, OTHER, line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db

_UTC_LATE = datetime.datetime(2026, 3, 1, 23, 30, tzinfo=datetime.UTC)


def _complete(invoice: AttendeeInvoice) -> Invoice:
    return calc.complete(mapping.invoice_draft_for(invoice), **mapping.complete_kwargs_for(invoice))


class TestEligibility:
    def test_eligible(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        assert mapping.ineligibility_reason(invoice) is None

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"status": AttendeeInvoice.InvoiceStatus.DRAFT}, "draft"),
            ({"status": AttendeeInvoice.InvoiceStatus.CANCELLED}, "cancelled"),
            ({"totals_method": TotalsMethod.LEGACY}, "legacy_totals"),
            ({"seller_vat_id": ""}, "missing_seller_vat_id"),
            ({"seller_country": ""}, "missing_seller_country"),
            ({"buyer_country": ""}, "missing_buyer_country"),
            ({"issued_at": None, "status": AttendeeInvoice.InvoiceStatus.ISSUED}, "missing_issued_at"),
        ],
    )
    def test_reasons(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        overrides: dict[str, object],
        reason: str,
    ) -> None:
        kwargs: dict[str, object] = {"lines": [line("24.90", "13.00")], **overrides}
        invoice = make_invoice(organization, event, member_user, **kwargs)  # type: ignore[arg-type]
        if "issued_at" in overrides:
            AttendeeInvoice.objects.filter(pk=invoice.pk).update(issued_at=None)
            invoice.refresh_from_db()
        assert mapping.ineligibility_reason(invoice) == reason

    def test_reverse_charge_needs_buyer_vat_id(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization, event, member_user, lines=[line("100.00", "0.00", "AE")], reverse_charge=True, buyer_vat_id=""
        )
        assert mapping.ineligibility_reason(invoice) == "reverse_charge_without_buyer_vat_id"

    def test_line_without_category(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        del invoice.line_items[0]["vat_category"]
        invoice.save()
        assert mapping.ineligibility_reason(invoice) == "line_without_category"

    def test_credit_note_follows_its_invoice_and_its_own_method(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        eligible = make_credit_note(invoice, [line("24.90", "13.00")])
        assert mapping.credit_note_ineligibility_reason(eligible) is None
        legacy = make_credit_note(invoice, [line("24.90", "13.00")], totals_method=TotalsMethod.LEGACY)
        assert mapping.credit_note_ineligibility_reason(legacy) == "legacy_totals"
        invoice.seller_vat_id = ""
        invoice.save()
        assert mapping.credit_note_ineligibility_reason(eligible) == "missing_seller_vat_id"

    def test_credit_note_of_a_cancelled_invoice_is_eligible(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """A full credit note cancels its invoice before the note's PDF is rendered."""
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        note = make_credit_note(invoice, [line("24.90", "13.00")])
        invoice.status = AttendeeInvoice.InvoiceStatus.CANCELLED
        invoice.save()
        assert mapping.credit_note_ineligibility_reason(note) is None


class TestHeader:
    def test_bt_1_2_3_5(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], issued_at=_UTC_LATE)
        draft = mapping.invoice_draft_for(invoice)
        assert draft.number == invoice.invoice_number
        assert draft.issue_date == datetime.date(2026, 3, 1)  # no event city: UTC
        assert draft.type_code == "380"
        assert draft.currency_code == "EUR"
        assert draft.process_control.specification_identifier == "urn:cen.eu:en16931:2017"

    def test_issue_date_in_event_timezone(
        self, organization: Organization, event: Event, member_user: RevelUser, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mapping, "get_event_timezone", lambda event: ZoneInfo("Europe/Vienna"))
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], issued_at=_UTC_LATE)
        assert mapping.invoice_draft_for(invoice).issue_date == datetime.date(2026, 3, 2)

    def test_issue_date_without_event_is_utc(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """A deleted event leaves ``event`` NULL (SET_NULL); the date then falls back to UTC."""
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], issued_at=_UTC_LATE)
        AttendeeInvoice.objects.filter(pk=invoice.pk).update(event=None)
        invoice.refresh_from_db()
        assert mapping.invoice_draft_for(invoice).issue_date == datetime.date(2026, 3, 1)

    def test_parties(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        draft = mapping.invoice_draft_for(invoice)
        seller, buyer = draft.seller, draft.buyer
        assert seller.name == "ACME GmbH"
        assert seller.vat_identifier == "DE811234567"  # BT-31
        assert seller.postal_address.address_line_1 == "Musterstraße 1"
        assert seller.postal_address.post_code == "10115"
        assert seller.postal_address.city == "Berlin"
        assert seller.postal_address.country_code == "DE"  # BT-40
        assert seller.contact is not None and seller.contact.email == "billing@acme.example"
        assert buyer.name == "Buyer GmbH"
        assert buyer.vat_identifier == "DE123456789"  # BT-48
        assert buyer.postal_address.country_subdivision == "BE"
        assert buyer.postal_address.country_code == "DE"  # BT-55

    def test_blank_optionals_are_omitted(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization, event, member_user, lines=[line("24.90", "13.00")], buyer_vat_id="", seller_email=""
        )
        draft = mapping.invoice_draft_for(invoice)
        assert draft.buyer.vat_identifier is None
        assert draft.seller.contact is None
        assert draft.seller.postal_address.address_line_2 is None

    def test_supply_date_only(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        delivery = mapping.invoice_draft_for(invoice).delivery
        assert delivery is not None
        assert delivery.actual_delivery_date == datetime.date(2026, 3, 14)  # BT-72
        assert delivery.invoicing_period is None

    def test_supply_period(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "13.00")],
            supply_period_end=datetime.date(2026, 3, 16),
        )
        delivery = mapping.invoice_draft_for(invoice).delivery
        assert delivery is not None and delivery.invoicing_period is not None
        assert delivery.actual_delivery_date == datetime.date(2026, 3, 14)
        assert delivery.invoicing_period.start_date == datetime.date(2026, 3, 14)  # BT-73
        assert delivery.invoicing_period.end_date == datetime.date(2026, 3, 16)  # BT-74

    def test_no_supply_date(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], supply_date=None)
        assert mapping.invoice_draft_for(invoice).delivery is None


class TestPayment:
    def test_card(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], payment=CARD)
        payment = mapping.invoice_draft_for(invoice).payment_instructions
        assert payment is not None
        assert payment.payment_means_type_code == "48"  # BT-81
        assert payment.payment_means_text == "Paid by card"  # BT-82
        assert payment.payment_card is not None and payment.payment_card.primary_account_number == "4242"  # BT-87

    def test_other(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], payment=OTHER)
        payment = mapping.invoice_draft_for(invoice).payment_instructions
        assert payment is not None
        assert payment.payment_means_type_code == "ZZZ"
        assert payment.payment_means_text == "Paid online via Stripe (PayPal)"
        assert payment.payment_card is None

    def test_none_when_unknown(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization, event, member_user, lines=[line("24.90", "13.00")], payment_means_code="", card_last4=""
        )
        assert mapping.invoice_draft_for(invoice).payment_instructions is None


class TestLines:
    def test_line_terms(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization, event, member_user, lines=[line("24.90", "13.00"), line("50.00", "20.00", description="VIP")]
        )
        complete = _complete(invoice)
        assert [ln.identifier for ln in complete.lines] == ["1", "2"]
        for ours, theirs in zip(invoice.line_items, complete.lines, strict=True):
            assert theirs.net_amount == Decimal(ours["net_amount"])  # BT-131 == our net exactly
            assert theirs.invoiced_quantity == 1
            assert theirs.invoiced_quantity_unit_code == "C62"
            assert theirs.item.name == ours["description"]
            assert theirs.vat_information.category_code == "S"  # BT-151
            assert theirs.vat_information.rate == Decimal(ours["vat_rate"])  # BT-152

    def test_out_of_scope_line_has_no_rate(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """BR-O-05: an O line carries no VAT rate (BT-152)."""
        invoice = make_invoice(
            organization, event, member_user, lines=[line("100.00", "0.00", "O")], buyer_country="US"
        )
        draft = mapping.invoice_draft_for(invoice)
        assert draft.lines[0].vat_information.rate is None
        # BR-O-02: no seller or buyer VAT identifier on a document not subject to VAT.
        assert draft.seller.vat_identifier is None
        assert draft.buyer.vat_identifier is None


class TestExemptions:
    def test_exempt_reason_text(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("100.00", "0.00", "E")],
            exempt_reason="Art. 132 VAT Directive",
        )
        (group,) = _complete(invoice).vat_breakdown
        assert group.category_code == "E"
        assert group.exemption_reason == "Art. 132 VAT Directive"  # BT-120
        assert group.exemption_reason_code is None

    def test_exempt_fallback_text(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("100.00", "0.00", "E")])
        (group,) = _complete(invoice).vat_breakdown
        assert group.exemption_reason == "Exempt from VAT"

    def test_out_of_scope_code(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization, event, member_user, lines=[line("100.00", "0.00", "O")], buyer_country="US"
        )
        (group,) = _complete(invoice).vat_breakdown
        assert group.exemption_reason_code == "VATEX-EU-O"  # BT-121
        assert group.rate is None

    def test_reverse_charge_code(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization, event, member_user, lines=[line("100.00", "0.00", "AE")], reverse_charge=True
        )
        (group,) = _complete(invoice).vat_breakdown
        assert group.exemption_reason_code == "VATEX-EU-AE"
        assert group.tax_amount == 0

    def test_standard_has_no_reason(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        (group,) = _complete(invoice).vat_breakdown
        assert group.exemption_reason is None and group.exemption_reason_code is None


class TestTotals:
    def test_paid_and_rounding(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """24.90 at 13 %: net 22.04, VAT 2.87, rounding -0.01 (BT-114); the buyer paid 24.90 (BT-113)."""
        invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
        assert invoice.rounding_amount == Decimal("-0.01")
        totals = _complete(invoice).totals
        assert totals.total_without_vat == Decimal("22.04")  # BT-109
        assert totals.total_vat == Decimal("2.87")  # BT-110
        assert totals.total_with_vat == Decimal("24.91")  # BT-112
        assert totals.paid_amount == Decimal("24.90")  # BT-113
        assert totals.rounding_amount == Decimal("-0.01")  # BT-114
        assert totals.amount_due == 0  # BT-115

    def test_zero_rounding_is_omitted(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(organization, event, member_user, lines=[line("100.00", "20.00")])
        assert mapping.complete_kwargs_for(invoice)["rounding_amount"] is None


class TestCreditNote:
    def test_credit_note_draft(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "13.00"), line("24.90", "13.00")],
            issued_at=datetime.datetime(2026, 3, 1, 10, 0, tzinfo=datetime.UTC),
        )
        note = make_credit_note(invoice, [line("24.90", "13.00", description="Refund — Event — Tier — Guest")])
        draft = mapping.credit_note_draft_for(note)
        assert draft.number == note.credit_note_number
        assert draft.type_code == "381"
        (reference,) = draft.preceding_invoice_references
        assert reference.reference == invoice.invoice_number  # BT-25
        assert reference.issue_date == datetime.date(2026, 3, 1)  # BT-26
        assert draft.seller.vat_identifier == invoice.seller_vat_id
        assert draft.payment_instructions is not None and draft.payment_instructions.payment_means_type_code == "48"
        complete = calc.complete(draft, **mapping.complete_kwargs_for(note))
        assert complete.lines[0].item.name == "Refund — Event — Tier — Guest"
        assert complete.totals.total_without_vat == note.amount_net
        assert complete.totals.total_vat == note.amount_vat
        assert complete.totals.paid_amount == note.amount_gross
        assert complete.totals.rounding_amount == (note.rounding_amount or None)
        assert complete.totals.amount_due == 0

    def test_credit_note_uses_invoice_exemptions(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("100.00", "0.00", "E")],
            exempt_reason="Art. 132 VAT Directive",
        )
        note = make_credit_note(invoice, [line("40.00", "0.00", "E")])
        complete = calc.complete(mapping.credit_note_draft_for(note), **mapping.complete_kwargs_for(note))
        assert complete.vat_breakdown[0].exemption_reason == "Art. 132 VAT Directive"
