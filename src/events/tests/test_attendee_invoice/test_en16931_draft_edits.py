"""DRAFT edits and the issuance guard follow the invoice's totals method (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, TotalsMethod
from events.service.attendee_invoice_draft_service import issue_draft_invoice, update_draft_invoice
from events.tests.test_attendee_invoice._helpers import MOCK_RENDER_PDF, _create_draft

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"):
        yield


def _en16931_draft(organization: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    """The shared fixture row (100.00 at 22%: 81.97 + 18.03, reachable) stamped en16931."""
    invoice = _create_draft(organization, event, user)
    invoice.totals_method = TotalsMethod.EN16931
    invoice.save(update_fields=["totals_method"])
    return invoice


# A line whose submitted net/VAT are nonsense: only gross, rate and description may matter on en16931.
_NONSENSE_LINE = {
    "description": "Ticket",
    "unit_price_gross": Decimal("24.90"),
    "discount_amount": Decimal("0.00"),
    "net_amount": Decimal("1.00"),
    "vat_amount": Decimal("2.00"),
    "vat_rate": Decimal("13.00"),
}


class TestDraftEdits:
    def test_legacy_draft_edit_unchanged(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Review focus 3: a legacy row keeps today's behaviour -- the header is the plain sum of the lines."""
        invoice = _create_draft(organization, event, member_user)
        updated = update_draft_invoice(invoice, {"line_items": [dict(_NONSENSE_LINE)]})
        updated.refresh_from_db()
        assert updated.totals_method == "legacy"
        assert updated.line_items[0]["net_amount"] == "1.00"
        assert updated.line_items[0]["vat_amount"] == "2.00"
        assert (updated.total_gross, updated.total_net, updated.total_vat) == (
            Decimal("24.90"),
            Decimal("1.00"),
            Decimal("2.00"),
        )
        assert updated.rounding_amount == 0

    def test_en16931_draft_edit_recomputes_net_vat(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _en16931_draft(organization, event, member_user)
        updated = update_draft_invoice(invoice, {"line_items": [dict(_NONSENSE_LINE)]})
        updated.refresh_from_db()
        assert updated.line_items[0]["net_amount"] == "22.04"
        assert updated.line_items[0]["vat_amount"] == "2.87"
        assert updated.line_items[0]["unit_price_gross"] == "24.90"
        assert (updated.total_gross, updated.total_net, updated.total_vat) == (
            Decimal("24.90"),
            Decimal("22.04"),
            Decimal("2.87"),
        )
        assert updated.rounding_amount == Decimal("-0.01")
        assert updated.total_net + updated.total_vat + updated.rounding_amount == updated.total_gross

    def test_reverse_charge_only_patch_on_en16931_keeps_invariant(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _en16931_draft(organization, event, member_user)
        zero_line = {**invoice.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "net_amount": "100.00"}
        updated = update_draft_invoice(invoice, {"line_items": [zero_line]})
        updated = update_draft_invoice(updated, {"reverse_charge": True})
        updated.refresh_from_db()
        assert updated.reverse_charge is True
        assert updated.line_items[0]["vat_category"] == "AE"
        assert updated.line_items[0]["net_amount"] == "100.00"
        assert (updated.total_net, updated.total_vat, updated.rounding_amount) == (
            Decimal("100.00"),
            Decimal("0.00"),
            Decimal("0.00"),
        )
        assert updated.total_net + updated.total_vat + updated.rounding_amount == updated.total_gross


class TestIssuanceGuard:
    def test_issue_en16931_draft_reconciles(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _en16931_draft(organization, event, member_user)
        invoice = update_draft_invoice(invoice, {"line_items": [dict(_NONSENSE_LINE)]})
        assert invoice.rounding_amount == Decimal("-0.01")
        assert issue_draft_invoice(invoice).status == AttendeeInvoice.InvoiceStatus.ISSUED

    @pytest.mark.parametrize(
        ("field", "value"),
        [("total_vat", Decimal("18.04")), ("rounding_amount", Decimal("0.01"))],
    )
    def test_issue_rejects_desynced_en16931_header(
        self, organization: Organization, event: Event, member_user: RevelUser, field: str, value: Decimal
    ) -> None:
        invoice = _en16931_draft(organization, event, member_user)
        AttendeeInvoice.objects.filter(pk=invoice.pk).update(**{field: value})
        invoice.refresh_from_db()
        with pytest.raises(HttpError) as exc_info:
            issue_draft_invoice(invoice)
        assert exc_info.value.status_code == 422
        assert field in str(exc_info.value)
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT

    def test_legacy_guard_ignores_rounding(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """A legacy row is judged on its three sums only; its rounding column is not part of the method."""
        invoice = _create_draft(organization, event, member_user)
        assert issue_draft_invoice(invoice).status == AttendeeInvoice.InvoiceStatus.ISSUED
