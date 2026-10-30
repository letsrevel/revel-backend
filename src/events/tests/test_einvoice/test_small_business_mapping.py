"""EN 16931 mapping of small-business sellers identified by a tax registration number (BT-32), #1244."""

import typing as t
from decimal import Decimal

import pytest
from euinvoice import calc

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.service import einvoice_mapping as mapping
from events.tests.test_einvoice._fixtures import line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db

DE_REASON = "Steuerbefreit gemäß § 19 UStG (Kleinunternehmerregelung)"
STEUERNUMMER = "12/345/67890"


def _small_business_invoice(
    organization: Organization, event: Event, user: RevelUser, **overrides: t.Any
) -> AttendeeInvoice:
    kwargs: dict[str, t.Any] = {
        "lines": [line("24.90", "0.00", "E"), line("50.00", "0.00", "E", "VIP")],
        "seller_vat_id": "",
        "seller_tax_registration_number": STEUERNUMMER,
        "buyer_vat_id": "",
        "exempt_reason": DE_REASON,
        **overrides,
    }
    return make_invoice(organization, event, user, **kwargs)


class TestEligibility:
    def test_tax_number_stands_in_for_the_vat_id(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _small_business_invoice(organization, event, member_user)
        assert mapping.ineligibility_reason(invoice) is None

    def test_neither_identifier_is_ineligible(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _small_business_invoice(organization, event, member_user, seller_tax_registration_number="")
        assert mapping.ineligibility_reason(invoice) == "missing_seller_vat_id"

    def test_reverse_charge_needs_the_seller_vat_id(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """BR-AE-02: BT-32 is not enough for a reverse-charge line."""
        invoice = _small_business_invoice(
            organization,
            event,
            member_user,
            lines=[line("100.00", "0.00", "AE")],
            reverse_charge=True,
            buyer_vat_id="AT1",
        )
        assert mapping.ineligibility_reason(invoice) == "reverse_charge_without_seller_vat_id"

    def test_credit_note_follows_the_invoice(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _small_business_invoice(organization, event, member_user)
        note = make_credit_note(invoice, [line("24.90", "0.00", "E")])
        assert mapping.credit_note_ineligibility_reason(note) is None


class TestSellerMapping:
    def test_bt32_and_bt29_without_bt31(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        invoice = _small_business_invoice(organization, event, member_user)
        seller = mapping.invoice_draft_for(invoice).seller
        assert seller.vat_identifier is None
        assert seller.tax_registration_identifier == STEUERNUMMER
        assert [i.value for i in seller.identifiers] == [STEUERNUMMER]  # BR-CO-26

    def test_vat_id_seller_keeps_bt31_and_adds_bt32_when_held(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "13.00")],
            seller_tax_registration_number=STEUERNUMMER,
        )
        seller = mapping.invoice_draft_for(invoice).seller
        assert seller.vat_identifier == "DE811234567"
        assert seller.tax_registration_identifier == STEUERNUMMER
        assert seller.identifiers == ()

    def test_exempt_breakdown_carries_the_statutory_note(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _small_business_invoice(organization, event, member_user)
        completed = calc.complete(mapping.invoice_draft_for(invoice), **mapping.complete_kwargs_for(invoice))
        [group] = completed.vat_breakdown
        assert (group.category_code, group.rate, group.tax_amount) == ("E", Decimal("0"), Decimal("0"))
        assert group.exemption_reason == DE_REASON  # BR-E-10
        assert completed.totals.total_with_vat == completed.totals.total_without_vat
