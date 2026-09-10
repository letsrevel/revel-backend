"""Attendee-invoice generation tests: prerequisites, invoicing mode, generation, export.

Tests cover:
- validate_invoicing_prerequisites() -- all 4 prerequisite checks
- set_invoicing_mode() -- NONE always allowed, HYBRID/AUTO require prerequisites
- generate_attendee_invoice() -- HYBRID/AUTO modes, idempotency, edge cases
- _is_export() -- non-EU / reverse-charge classification

Draft lifecycle tests live in test_lifecycle.py; credit-note tests in
test_attendee_credit_note_service.py.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_service import (
    _is_export,
    generate_attendee_invoice,
    set_invoicing_mode,
    validate_invoicing_prerequisites,
)
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# validate_invoicing_prerequisites
# ---------------------------------------------------------------------------


class TestValidateInvoicingPrerequisites:
    """Test all 4 prerequisite checks for enabling invoicing."""

    def test_passes_when_all_prerequisites_met(self, organization: Organization) -> None:
        """No error raised when org has all required fields."""
        validate_invoicing_prerequisites(_make_org_invoicing_ready(organization))

    def test_fails_when_vat_country_code_missing(self, organization: Organization) -> None:
        """Missing EU VAT country code should fail."""
        org = _make_org_invoicing_ready(organization)
        org.vat_country_code = ""
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert exc_info.value.status_code == 422
        assert "EU-based" in str(exc_info.value)

    def test_fails_when_vat_country_code_non_eu(self, organization: Organization) -> None:
        """Non-EU VAT country code should fail."""
        org = _make_org_invoicing_ready(organization)
        org.vat_country_code = "US"
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert exc_info.value.status_code == 422

    def test_fails_when_vat_id_not_validated(self, organization: Organization) -> None:
        """Unvalidated VAT ID should fail."""
        org = _make_org_invoicing_ready(organization)
        org.vat_id_validated = False
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert "VIES" in str(exc_info.value)

    def test_fails_when_billing_name_missing(self, organization: Organization) -> None:
        """Missing billing name should fail."""
        org = _make_org_invoicing_ready(organization)
        org.billing_name = ""
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert "Billing name" in str(exc_info.value)

    def test_fails_when_billing_address_missing(self, organization: Organization) -> None:
        """Missing billing address should fail."""
        org = _make_org_invoicing_ready(organization)
        org.billing_address = ""
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert "Billing address" in str(exc_info.value)

    def test_reports_all_missing_fields_at_once(self, organization: Organization) -> None:
        """All missing prerequisites should be reported in a single error."""
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(organization)
        msg = str(exc_info.value)
        for expected in ("EU-based", "VIES", "Billing name", "Billing address"):
            assert expected in msg


# ---------------------------------------------------------------------------
# set_invoicing_mode
# ---------------------------------------------------------------------------


class TestSetInvoicingMode:
    """Test invoicing mode transitions."""

    def test_set_none_always_allowed(self, organization: Organization) -> None:
        """Setting NONE requires no prerequisites."""
        result = set_invoicing_mode(organization, Organization.InvoicingMode.NONE)
        assert result.invoicing_mode == Organization.InvoicingMode.NONE

    def test_set_hybrid_requires_prerequisites(self, organization: Organization) -> None:
        """Setting HYBRID without prerequisites should raise 422."""
        with pytest.raises(HttpError) as exc_info:
            set_invoicing_mode(organization, Organization.InvoicingMode.HYBRID)
        assert exc_info.value.status_code == 422

    def test_set_auto_requires_prerequisites(self, organization: Organization) -> None:
        """Setting AUTO without prerequisites should raise 422."""
        with pytest.raises(HttpError):
            set_invoicing_mode(organization, Organization.InvoicingMode.AUTO)

    def test_set_hybrid_with_prerequisites(self, organization: Organization) -> None:
        """Setting HYBRID succeeds when org meets all prerequisites."""
        org = _make_org_invoicing_ready(organization)
        result = set_invoicing_mode(org, Organization.InvoicingMode.HYBRID)
        assert result.invoicing_mode == Organization.InvoicingMode.HYBRID
        org.refresh_from_db()
        assert org.invoicing_mode == Organization.InvoicingMode.HYBRID

    def test_set_auto_with_prerequisites(self, organization: Organization) -> None:
        """Setting AUTO succeeds when org meets all prerequisites."""
        org = _make_org_invoicing_ready(organization)
        result = set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
        assert result.invoicing_mode == Organization.InvoicingMode.AUTO

    def test_downgrade_from_auto_to_none(self, organization: Organization) -> None:
        """Downgrading from AUTO to NONE should always work."""
        org = _make_org_invoicing_ready(organization)
        set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
        result = set_invoicing_mode(org, Organization.InvoicingMode.NONE)
        assert result.invoicing_mode == Organization.InvoicingMode.NONE


# ---------------------------------------------------------------------------
# generate_attendee_invoice
# ---------------------------------------------------------------------------


class TestGenerateAttendeeInvoice:
    """Test invoice generation for completed checkout sessions."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_hybrid_mode_creates_draft(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """HYBRID mode should create an invoice with DRAFT status."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_hybrid_1",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_hybrid_1")
        assert invoice is not None
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT
        assert invoice.issued_at is None
        assert invoice.organization == org
        assert invoice.user == member_user

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_auto_mode_creates_issued_invoice(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """AUTO mode should create an invoice with ISSUED status and issued_at set."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_auto_1",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_auto_1")
        assert invoice is not None
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED
        assert invoice.issued_at is not None

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_no_billing_snapshot_returns_none(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """No buyer billing snapshot should return None."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_no_billing",
            buyer_billing_snapshot=None,
        )
        assert generate_attendee_invoice("cs_no_billing") is None

    def test_org_invoicing_mode_none_returns_none(
        self,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Org with invoicing_mode=NONE should not generate an invoice."""
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_no_inv",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        assert generate_attendee_invoice("cs_no_inv") is None

    def test_no_payments_returns_none(self) -> None:
        """No payments for the session should return None."""
        assert generate_attendee_invoice("cs_nonexistent") is None

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_idempotency_returns_same_invoice(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Calling generate twice for the same session returns the same invoice."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_idemp",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        inv1 = generate_attendee_invoice("cs_idemp")
        inv2 = generate_attendee_invoice("cs_idemp")
        assert inv1 is not None and inv2 is not None
        assert inv1.id == inv2.id

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_line_items_built_correctly(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Line items should contain event name, tier name, and guest name."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_li",
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="John Doe",
        )
        invoice = generate_attendee_invoice("cs_li")
        assert invoice is not None
        assert len(invoice.line_items) == 1
        item = invoice.line_items[0]
        assert event.name in item["description"]
        assert event_ticket_tier.name in item["description"]
        assert "John Doe" in item["description"]

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_line_item_description_omits_blank_holder_name(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A blank holder name (#845) must not leave a trailing separator on the invoice line."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_li_blank",
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="",
        )
        invoice = generate_attendee_invoice("cs_li_blank")
        assert invoice is not None
        assert invoice.line_items[0]["description"] == f"{event.name} — {event_ticket_tier.name}"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_seller_and_buyer_snapshots(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Invoice should snapshot seller (org) and buyer info correctly."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        snapshot = _default_billing_snapshot()
        _create_payment(
            user=member_user, event=event, tier=event_ticket_tier, session_id="cs_snap", buyer_billing_snapshot=snapshot
        )
        invoice = generate_attendee_invoice("cs_snap")
        assert invoice is not None
        assert invoice.seller_name == org.billing_name
        assert invoice.seller_vat_id == org.vat_id
        assert invoice.buyer_name == snapshot["billing_name"]
        assert invoice.buyer_email == snapshot["billing_email"]

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_reverse_charge_detected(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Reverse charge flag set when billing snapshot records reverse_charge=True.

        Snapshot passthrough only: checkout no longer produces reverse_charge=True
        snapshots (#868 — admission is taxed where the event takes place), but the
        invoice service still honors historical snapshots on webhook replays.
        """
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_rc",
            amount=Decimal("81.97"),
            net_amount=Decimal("81.97"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            buyer_billing_snapshot=_default_billing_snapshot(reverse_charge=True),
        )
        invoice = generate_attendee_invoice("cs_rc")
        assert invoice is not None
        assert invoice.reverse_charge is True
        assert invoice.total_vat == Decimal("0.00")

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_totals_aggregated_from_multiple_payments(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Invoice totals should be aggregated from all payments in the session."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        for i in range(2):
            _create_payment(
                user=member_user,
                event=event,
                tier=event_ticket_tier,
                session_id="cs_multi",
                buyer_billing_snapshot=_default_billing_snapshot(),
                guest_name=f"Guest {i}",
            )
        invoice = generate_attendee_invoice("cs_multi")
        assert invoice is not None
        assert invoice.total_gross == Decimal("200.00")
        assert invoice.total_net == Decimal("163.94")
        assert len(invoice.line_items) == 2


# ---------------------------------------------------------------------------
# _is_export — PDF export wording gate
# ---------------------------------------------------------------------------


class TestIsExport:
    """Export wording is gated on total_vat == 0 (#868).

    Admission is never zero-rated as an export anymore, so only historical
    zero-rated documents (which must re-render as issued) get the export
    wording; new non-EU invoices carry full VAT and render normally.
    """

    @staticmethod
    def _invoice(**overrides: t.Any) -> AttendeeInvoice:
        """An unsaved invoice shaped like a historical non-EU zero-rated document."""
        fields: dict[str, t.Any] = {
            "buyer_vat_country": "US",
            "reverse_charge": False,
            "total_vat": Decimal("0.00"),
        }
        fields.update(overrides)
        return AttendeeInvoice(**fields)

    def test_non_eu_invoice_with_vat_is_not_export(self) -> None:
        """A new non-EU invoice carrying full VAT (#868) must not render export wording."""
        assert _is_export(self._invoice(total_vat=Decimal("18.03"))) is False

    def test_historical_zero_rated_non_eu_invoice_is_export(self) -> None:
        """A historical document (total_vat=0, non-EU buyer, not RC) still renders as export."""
        assert _is_export(self._invoice()) is True

    def test_reverse_charge_invoice_is_not_export(self) -> None:
        """Reverse-charge documents get RC wording, never export wording."""
        assert _is_export(self._invoice(reverse_charge=True)) is False

    def test_eu_buyer_zero_vat_invoice_is_not_export(self) -> None:
        """A zero-VAT invoice for an EU buyer (e.g. 0% org rate) is not an export."""
