"""EN 16931 VAT category on invoice lines, exemption snapshot and PDF notice (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.service.attendee_invoice_draft_service import update_draft_invoice
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = pytest.mark.django_db

REASON = "§4 Nr. 20 UStG"


def _auto_org(organization: Organization) -> Organization:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return org


def _exempt_tier(event: Event, reason: str = REASON) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Exempt",
        price=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
        vat_exemption_reason=reason,
    )


def _exempt_payment(user: RevelUser, event: Event, tier: TicketTier, session_id: str, **kw: t.Any) -> t.Any:
    return _create_payment(
        user=user,
        event=event,
        tier=tier,
        session_id=session_id,
        amount=Decimal("100.00"),
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("0.00"),
        vat_rate=Decimal("0.00"),
        buyer_billing_snapshot=_default_billing_snapshot(**kw),
    )


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
class TestGeneration:
    def test_lines_carry_categories_for_mixed_cart(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _auto_org(organization)
        _exempt_payment(member_user, event, _exempt_tier(event), "cs_mixed")
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_mixed",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_mixed")
        assert invoice is not None
        assert [li["vat_category"] for li in invoice.line_items] == ["E", "S"]
        assert invoice.vat_exemptions == {"E": {"text": REASON, "code": ""}}

    def test_reverse_charge_lines_are_ae(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _auto_org(organization)
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_rc",
            vat_amount=Decimal("0.00"),
            net_amount=Decimal("100.00"),
            vat_rate=Decimal("0.00"),
            buyer_billing_snapshot=_default_billing_snapshot(reverse_charge=True),
        )
        invoice = generate_attendee_invoice("cs_rc")
        assert invoice is not None
        assert [li["vat_category"] for li in invoice.line_items] == ["AE"]
        assert invoice.vat_exemptions == {"AE": {"text": "Reverse charge", "code": "VATEX-EU-AE"}}

    def test_exempt_line_without_any_reason_uses_fallback(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _auto_org(organization)
        _exempt_payment(member_user, event, event_ticket_tier, "cs_legacy_e")
        invoice = generate_attendee_invoice("cs_legacy_e")
        assert invoice is not None
        assert invoice.vat_exemptions == {"E": {"text": "Exempt from VAT", "code": ""}}

    def test_credit_note_lines_carry_category(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        _auto_org(organization)
        payment = _exempt_payment(member_user, event, _exempt_tier(event), "cs_cn")
        assert generate_attendee_invoice("cs_cn") is not None
        cn = generate_attendee_credit_note("cs_cn", [payment.id])
        assert cn is not None
        assert [li["vat_category"] for li in cn.line_items] == ["E"]

    def test_pdf_prints_exempt_reason_once(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        _auto_org(organization)
        _exempt_payment(member_user, event, _exempt_tier(event), "cs_pdf")
        invoice = generate_attendee_invoice("cs_pdf")
        assert invoice is not None
        html = render_to_string("invoices/attendee_invoice.html", {"invoice": invoice})
        assert html.count(REASON) == 1


class TestDraftEdits:
    def test_legacy_invoice_without_categories_renders_and_edits(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        inv = _create_draft(organization, event, member_user)
        assert "vat_category" not in inv.line_items[0]
        assert inv.vat_exemptions == {}
        html = render_to_string("invoices/attendee_invoice.html", {"invoice": inv})
        assert "VAT exempt" not in html
        updated = update_draft_invoice(inv, {"line_items": [{**inv.line_items[0], "description": "Edited"}]})
        assert updated.line_items[0]["vat_category"] == "S"

    def test_draft_edit_recomputes_categories(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        organization.vat_exemption_reason = "Art. 132"
        organization.save()
        inv = _create_draft(organization, event, member_user)
        line = {
            "description": "Ticket",
            "unit_price_gross": Decimal("100.00"),
            "discount_amount": Decimal("0.00"),
            "net_amount": Decimal("100.00"),
            "vat_amount": Decimal("0.00"),
            "vat_rate": Decimal("0.00"),
        }
        updated = update_draft_invoice(inv, {"line_items": [line]})
        updated.refresh_from_db()
        assert updated.line_items[0]["vat_category"] == "E"
        assert updated.vat_exemptions == {"E": {"text": "Art. 132", "code": ""}}

    def test_draft_edit_keeps_stored_exempt_text(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        inv = _create_draft(organization, event, member_user)
        inv.vat_exemptions = {"E": {"text": REASON, "code": ""}}
        inv.save()
        line = {
            "description": "T",
            "unit_price_gross": Decimal("5"),
            "net_amount": Decimal("5"),
            "vat_rate": Decimal("0"),
        }
        updated = update_draft_invoice(inv, {"line_items": [line]})
        assert updated.vat_exemptions["E"]["text"] == REASON

    def test_toggling_reverse_charge_recomputes(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        inv = _create_draft(organization, event, member_user)
        updated = update_draft_invoice(inv, {"reverse_charge": True, "line_items": [inv.line_items[0]]})
        assert updated.line_items[0]["vat_category"] == "AE"
        assert "AE" in updated.vat_exemptions
