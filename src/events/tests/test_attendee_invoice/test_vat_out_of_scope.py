"""Out-of-EU-scope virtual sales are tagged O (not E) on invoices, credit notes and the PDF (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.service.attendee_invoice_service import (
    _is_export,
    generate_attendee_credit_note,
    generate_attendee_invoice,
)
from events.service.attendee_vat_service import is_outside_eu_vat_scope
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.utils.vat_category import VatCategory, build_vat_exemptions, derive_vat_category

pytestmark = pytest.mark.django_db

O_EXEMPTION = {"text": "Not subject to EU VAT (place of supply outside the EU)", "code": "VATEX-EU-O"}


def test_derive_and_exemptions_for_out_of_scope() -> None:
    zero = Decimal("0")
    assert derive_vat_category(zero, False, out_of_scope=True) == VatCategory.OUT_OF_SCOPE
    assert derive_vat_category(zero, True, out_of_scope=True) == VatCategory.REVERSE_CHARGE
    assert derive_vat_category(Decimal("20"), False, out_of_scope=True) == VatCategory.STANDARD
    assert derive_vat_category(zero, False) == VatCategory.EXEMPT
    assert build_vat_exemptions([VatCategory.OUT_OF_SCOPE], ["ignored"]) == {"O": O_EXEMPTION}


@pytest.mark.parametrize(
    ("is_virtual", "seller", "buyer", "expected"),
    [
        (True, "AT", "US", True),
        (False, "AT", "US", False),
        (True, "AT", "DE", False),
        (True, "AT", "", True),
        (True, "GR", "EL", False),
        (True, "AT", "AT", False),
    ],
)
def test_is_outside_eu_vat_scope(is_virtual: bool, seller: str, buyer: str, expected: bool) -> None:
    assert is_outside_eu_vat_scope(is_virtual, seller, buyer) is expected


def _setup(organization: Organization, event: Event, virtual: bool) -> None:
    org = _make_org_invoicing_ready(organization)
    org.vat_country_code = "AT"
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    event.is_virtual = virtual
    event.save()


def _payment(user: RevelUser, event: Event, tier: TicketTier, session: str, buyer_country: str, zero: bool) -> t.Any:
    snapshot = {**_default_billing_snapshot(), "vat_country_code": buyer_country, "vat_id": "", "billing_name": "B"}
    kw: dict[str, t.Any] = (
        {"net_amount": Decimal("100.00"), "vat_amount": Decimal("0.00"), "vat_rate": Decimal("0.00")} if zero else {}
    )
    return _create_payment(user=user, event=event, tier=tier, session_id=session, buyer_billing_snapshot=snapshot, **kw)


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
class TestOutOfScope:
    def test_virtual_non_eu_buyer_is_o_with_o_notice_pdf(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _setup(organization, event, virtual=True)
        _payment(member_user, event, event_ticket_tier, "cs_o", "US", zero=True)
        invoice = generate_attendee_invoice("cs_o")
        assert invoice is not None
        assert [li["vat_category"] for li in invoice.line_items] == ["O"]
        assert invoice.vat_exemptions == {"O": O_EXEMPTION}
        assert _is_export(invoice) is False
        assert invoice.out_of_scope_text == O_EXEMPTION["text"]
        html = render_to_string("invoices/attendee_invoice.html", {"invoice": invoice, "is_export": False})
        assert O_EXEMPTION["text"] in html
        assert "Export of services" not in html
        assert "Exempt from VAT" not in html
        assert "VAT exempt" not in html

    def test_virtual_eu_b2c_cross_border_stays_standard(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _setup(organization, event, virtual=True)
        _payment(member_user, event, event_ticket_tier, "cs_eu", "DE", zero=False)
        invoice = generate_attendee_invoice("cs_eu")
        assert invoice is not None
        assert [li["vat_category"] for li in invoice.line_items] == ["S"]
        assert _is_export(invoice) is False

    def test_physical_event_us_buyer_stays_standard(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _setup(organization, event, virtual=False)
        _payment(member_user, event, event_ticket_tier, "cs_phys", "US", zero=False)
        invoice = generate_attendee_invoice("cs_phys")
        assert invoice is not None
        assert [li["vat_category"] for li in invoice.line_items] == ["S"]

    def test_credit_note_for_out_of_scope_invoice_is_o(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _setup(organization, event, virtual=True)
        payment = _payment(member_user, event, event_ticket_tier, "cs_cn_o", "US", zero=True)
        assert generate_attendee_invoice("cs_cn_o") is not None
        cn = generate_attendee_credit_note("cs_cn_o", [payment.id])
        assert cn is not None
        assert [li["vat_category"] for li in cn.line_items] == ["O"]
        assert cn.has_out_of_scope_lines is True
        html = render_to_string("invoices/attendee_credit_note.html", {"credit_note": cn, "invoice": cn.invoice})
        assert O_EXEMPTION["text"] in html
        assert "Export of services" not in html


class TestDraftBuyerCountryPatch:
    def test_buyer_country_only_patch_rederives_categories(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        from events.service.attendee_invoice_draft_service import update_draft_invoice
        from events.tests.test_attendee_invoice._helpers import _create_draft

        event.is_virtual = True
        event.save()
        organization.vat_country_code = "AT"
        organization.save()
        inv = _create_draft(organization, event, member_user)
        inv.seller_vat_country = "AT"
        inv.buyer_vat_country = "AT"
        inv.line_items = [{**inv.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "vat_category": "S"}]
        inv.save()

        updated = update_draft_invoice(inv, {"buyer_vat_country": "US"})

        updated.refresh_from_db()
        assert [li["vat_category"] for li in updated.line_items] == ["O"]
        assert "O" in updated.vat_exemptions


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
def test_credit_note_follows_the_invoice_not_current_event_state(
    _mail: t.Any,
    _pdf: t.Any,
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    _setup(organization, event, virtual=True)
    payment = _payment(member_user, event, event_ticket_tier, "cs_cn_flip", "US", zero=True)
    assert generate_attendee_invoice("cs_cn_flip") is not None
    event.is_virtual = False
    event.save()
    cn = generate_attendee_credit_note("cs_cn_flip", [payment.id])
    assert cn is not None
    assert [li["vat_category"] for li in cn.line_items] == ["O"]


def test_legacy_export_row_still_prints_export_wording(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    from events.tests.test_attendee_invoice._helpers import _create_draft

    inv = _create_draft(organization, event, member_user)
    inv.buyer_vat_country = "US"
    inv.total_vat = Decimal("0.00")
    inv.line_items = [{k: v for k, v in inv.line_items[0].items() if k != "vat_category"}]
    assert _is_export(inv) is True
    html = render_to_string("invoices/attendee_invoice.html", {"invoice": inv, "is_export": True})
    assert "Export of services" in html
