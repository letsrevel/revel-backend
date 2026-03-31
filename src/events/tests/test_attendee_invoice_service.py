"""Tests for the attendee invoice service: validation, generation, editing, credit notes.

Tests cover:
- validate_invoicing_prerequisites() -- all 4 prerequisite checks
- set_invoicing_mode() -- NONE always allowed, HYBRID/AUTO require prerequisites
- generate_attendee_invoice() -- HYBRID/AUTO modes, idempotency, edge cases
- update_draft_invoice() -- editable fields, disallowed fields, PDF invalidation
- issue_draft_invoice() -- DRAFT -> ISSUED transition
- delete_draft_invoice() -- draft deletion and issued rejection
- generate_attendee_credit_note() -- credit note lifecycle
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.models.ticket import Payment
from events.service.attendee_invoice_service import (
    delete_draft_invoice,
    generate_attendee_credit_note,
    generate_attendee_invoice,
    issue_draft_invoice,
    set_invoicing_mode,
    update_draft_invoice,
    validate_invoicing_prerequisites,
)

pytestmark = pytest.mark.django_db

MOCK_RENDER_PDF = "events.service.attendee_invoice_service.render_pdf"
MOCK_SEND_EMAIL = "common.tasks.send_email"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_org_invoicing_ready(org: Organization) -> Organization:
    """Configure an org with all prerequisites for invoicing."""
    org.vat_country_code = "IT"
    org.vat_id = "IT12345678901"
    org.vat_id_validated = True
    org.vat_rate = Decimal("22.00")
    org.billing_name = "ACME SRL"
    org.billing_address = "Via Roma 1, 00100 Roma"
    org.billing_email = "billing@acme.it"
    org.contact_email = "info@acme.it"
    org.save()
    return org


def _create_payment(
    *,
    user: RevelUser,
    event: Event,
    tier: TicketTier,
    session_id: str = "cs_test_123",
    amount: Decimal = Decimal("100.00"),
    net_amount: Decimal | None = Decimal("81.97"),
    vat_amount: Decimal | None = Decimal("18.03"),
    vat_rate: Decimal | None = Decimal("22.00"),
    buyer_billing_snapshot: dict[str, t.Any] | None = None,
    guest_name: str = "Test Guest",
) -> Payment:
    """Create a ticket and payment for testing."""
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, guest_name=guest_name)
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session_id,
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=amount,
        net_amount=net_amount,
        vat_amount=vat_amount,
        vat_rate=vat_rate,
        platform_fee=Decimal("5.00"),
        currency="EUR",
        buyer_billing_snapshot=buyer_billing_snapshot,
    )


def _default_billing_snapshot(reverse_charge: bool = False) -> dict[str, t.Any]:
    return {
        "billing_name": "Buyer GmbH",
        "vat_id": "DE123456789",
        "vat_country_code": "DE",
        "vat_id_validated": True,
        "billing_address": "Berliner Str. 1, 10115 Berlin",
        "billing_email": "buyer@example.de",
        "reverse_charge": reverse_charge,
    }


_INVOICE_COUNTER = 0


def _create_draft(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    """Create a minimal draft invoice for testing."""
    global _INVOICE_COUNTER  # noqa: PLW0603
    _INVOICE_COUNTER += 1
    return AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id=f"cs_draft_{_INVOICE_COUNTER}",
        invoice_number=f"TEST-{_INVOICE_COUNTER:06d}",
        status=AttendeeInvoice.InvoiceStatus.DRAFT,
        total_gross=Decimal("100.00"),
        total_net=Decimal("81.97"),
        total_vat=Decimal("18.03"),
        vat_rate=Decimal("22.00"),
        currency="EUR",
        line_items=[],
        seller_name="ACME SRL",
        seller_email="billing@acme.it",
        buyer_name="Original Buyer",
        buyer_email="buyer@example.com",
    )


def _create_issued(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    """Create an issued invoice for testing."""
    inv = _create_draft(org, event, user)
    inv.status = AttendeeInvoice.InvoiceStatus.ISSUED
    inv.issued_at = timezone.now()
    inv.save()
    return inv


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
        """Reverse charge flag set when billing snapshot records reverse_charge=True."""
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
# update_draft_invoice
# ---------------------------------------------------------------------------


class TestUpdateDraftInvoice:
    """Test editing draft invoices."""

    def test_can_edit_buyer_name(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Editing buyer_name on a draft should succeed."""
        inv = _create_draft(organization, event, member_user)
        assert update_draft_invoice(inv, {"buyer_name": "New Name"}).buyer_name == "New Name"

    def test_can_edit_totals(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Editing financial totals on a draft should succeed."""
        inv = _create_draft(organization, event, member_user)
        updated = update_draft_invoice(inv, {"total_gross": Decimal("200.00"), "total_net": Decimal("163.93")})
        assert updated.total_gross == Decimal("200.00")

    def test_empty_update_returns_unchanged(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Empty update data should return the invoice unchanged."""
        inv = _create_draft(organization, event, member_user)
        assert update_draft_invoice(inv, {}).buyer_name == "Original Buyer"

    def test_cannot_edit_issued_invoice(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Attempting to edit an ISSUED invoice should raise 409."""
        inv = _create_issued(organization, event, member_user)
        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(inv, {"buyer_name": "Hacked"})
        assert exc_info.value.status_code == 409

    def test_cannot_edit_disallowed_fields(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Attempting to edit seller_name should raise 422."""
        inv = _create_draft(organization, event, member_user)
        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(inv, {"seller_name": "Hacked"})
        assert exc_info.value.status_code == 422
        assert "seller_name" in str(exc_info.value)

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_edit_invalidates_stale_pdf(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Editing a draft with an existing PDF should delete the stale PDF."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_stale",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_stale")
        assert invoice is not None and invoice.pdf_file
        update_draft_invoice(invoice, {"buyer_name": "Updated"})
        invoice.refresh_from_db()
        assert not invoice.pdf_file

    def test_all_editable_fields_accepted(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """All fields in EDITABLE_DRAFT_FIELDS should be accepted."""
        inv = _create_draft(organization, event, member_user)
        data: dict[str, t.Any] = {
            "buyer_name": "Updated Buyer",
            "buyer_vat_id": "DE123456789",
            "buyer_vat_country": "DE",
            "buyer_address": "Berlin",
            "buyer_email": "test@example.de",
            "total_gross": Decimal("99.99"),
            "total_net": Decimal("81.96"),
            "total_vat": Decimal("18.03"),
            "vat_rate": Decimal("22.00"),
            "currency": "USD",
            "reverse_charge": True,
            "discount_code_text": "SALE10",
            "discount_amount_total": Decimal("5.00"),
            "line_items": [{"description": "Test"}],
        }
        update_draft_invoice(inv, data)  # Should not raise


# ---------------------------------------------------------------------------
# issue_draft_invoice
# ---------------------------------------------------------------------------


class TestIssueDraftInvoice:
    """Test issuing a draft invoice."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_transitions_to_issued(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Issuing a draft should set status=ISSUED and issued_at."""
        inv = _create_draft(organization, event, member_user)
        result = issue_draft_invoice(inv)
        assert result.status == AttendeeInvoice.InvoiceStatus.ISSUED
        assert result.issued_at is not None
        mock_pdf.assert_called_once()

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_reissue_already_issued_is_idempotent(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Re-issuing an already-issued invoice should succeed (regenerate PDF)."""
        inv = _create_issued(organization, event, member_user)
        original_issued_at = inv.issued_at
        result = issue_draft_invoice(inv)
        assert result.status == AttendeeInvoice.InvoiceStatus.ISSUED
        assert result.issued_at == original_issued_at  # not re-set
        mock_pdf.assert_called_once()

    def test_cannot_issue_cancelled(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Issuing a cancelled invoice should raise 409."""
        inv = _create_draft(organization, event, member_user)
        inv.status = AttendeeInvoice.InvoiceStatus.CANCELLED
        inv.save()
        with pytest.raises(HttpError) as exc_info:
            issue_draft_invoice(inv)
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# delete_draft_invoice
# ---------------------------------------------------------------------------


class TestDeleteDraftInvoice:
    """Test draft invoice deletion."""

    def test_can_delete_draft(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Deleting a draft invoice should remove it from the database."""
        inv = _create_draft(organization, event, member_user)
        inv_id = inv.id
        delete_draft_invoice(inv)
        assert not AttendeeInvoice.objects.filter(id=inv_id).exists()

    def test_cannot_delete_issued(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Deleting an issued invoice should raise 409."""
        inv = _create_issued(organization, event, member_user)
        with pytest.raises(HttpError) as exc_info:
            delete_draft_invoice(inv)
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# generate_attendee_credit_note
# ---------------------------------------------------------------------------


class TestGenerateAttendeeCreditNote:
    """Test credit note generation for refunds."""

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_creates_credit_note_for_issued_invoice(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A credit note should be created for an issued invoice."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        payment = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_1",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_cn_1")
        assert invoice is not None
        cn = generate_attendee_credit_note("cs_cn_1", [payment.id])
        assert cn is not None
        assert cn.invoice == invoice
        assert cn.amount_gross == payment.amount
        assert cn.issued_at is not None

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_deletes_draft_instead_of_creating_credit_note(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Refunding a draft invoice should delete it, not create a credit note."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        payment = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_d",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_cn_d")
        assert invoice is not None
        inv_id = invoice.id
        result = generate_attendee_credit_note("cs_cn_d", [payment.id])
        assert result is None
        assert not AttendeeInvoice.objects.filter(id=inv_id).exists()

    def test_no_invoice_returns_none(self) -> None:
        """If no invoice exists for the session, return None."""
        assert generate_attendee_credit_note("cs_nonexistent", []) is None

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_full_credit_marks_invoice_cancelled(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Full credit note should mark the invoice as CANCELLED."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        payment = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_f",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_cn_f")
        assert invoice is not None
        generate_attendee_credit_note("cs_cn_f", [payment.id])
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_partial_refund_creates_credit_note_without_cancelling(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Partial refund should create a credit note but keep invoice ISSUED."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        p1 = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_partial",
            amount=Decimal("100.00"),
            net_amount=Decimal("81.97"),
            vat_amount=Decimal("18.03"),
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="Guest 1",
        )
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_partial",
            amount=Decimal("100.00"),
            net_amount=Decimal("81.97"),
            vat_amount=Decimal("18.03"),
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="Guest 2",
        )
        invoice = generate_attendee_invoice("cs_cn_partial")
        assert invoice is not None
        assert invoice.total_gross == Decimal("200.00")

        # Partial refund: only first payment
        cn = generate_attendee_credit_note("cs_cn_partial", [p1.id])
        assert cn is not None
        assert cn.amount_gross == Decimal("100.00")

        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_partial_then_remaining_refund_cancels_invoice(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Refunding remaining payments after partial refund should cancel invoice."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        p1 = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_seq",
            amount=Decimal("50.00"),
            net_amount=Decimal("40.98"),
            vat_amount=Decimal("9.02"),
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="Guest 1",
        )
        p2 = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_seq",
            amount=Decimal("50.00"),
            net_amount=Decimal("40.98"),
            vat_amount=Decimal("9.02"),
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="Guest 2",
        )
        invoice = generate_attendee_invoice("cs_cn_seq")
        assert invoice is not None

        cn1 = generate_attendee_credit_note("cs_cn_seq", [p1.id])
        assert cn1 is not None
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED

        cn2 = generate_attendee_credit_note("cs_cn_seq", [p2.id])
        assert cn2 is not None
        assert cn2.id != cn1.id
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_idempotent_credit_note_returns_same(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Calling generate_attendee_credit_note twice with same IDs returns same credit note."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        payment = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_cn_idem",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        invoice = generate_attendee_invoice("cs_cn_idem")
        assert invoice is not None

        cn1 = generate_attendee_credit_note("cs_cn_idem", [payment.id])
        cn2 = generate_attendee_credit_note("cs_cn_idem", [payment.id])
        assert cn1 is not None
        assert cn2 is not None
        assert cn1.id == cn2.id
        assert cn1.credit_note_number == cn2.credit_note_number

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_superset_retry_does_not_double_credit(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Webhook retry with superset of already-credited payment IDs must not double-credit.

        Scenario: batch purchase [A, B, C]. Refund A -> CN-1. Refund B -> CN-2.
        Stripe re-delivers with all three IDs [A, B, C]. Only C should be credited.
        """
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        payments = []
        for i in range(3):
            payments.append(
                _create_payment(
                    user=member_user,
                    event=event,
                    tier=event_ticket_tier,
                    session_id="cs_superset",
                    amount=Decimal("50.00"),
                    net_amount=Decimal("40.98"),
                    vat_amount=Decimal("9.02"),
                    buyer_billing_snapshot=_default_billing_snapshot(),
                    guest_name=f"Guest {i}",
                )
            )
        invoice = generate_attendee_invoice("cs_superset")
        assert invoice is not None

        # Step 1: refund A
        cn1 = generate_attendee_credit_note("cs_superset", [payments[0].id])
        assert cn1 is not None

        # Step 2: refund B
        cn2 = generate_attendee_credit_note("cs_superset", [payments[1].id])
        assert cn2 is not None
        assert cn2.id != cn1.id

        # Step 3: webhook retry sends superset [A, B, C]
        cn3 = generate_attendee_credit_note("cs_superset", [p.id for p in payments])
        assert cn3 is not None
        assert cn3.id != cn1.id
        assert cn3.id != cn2.id
        # Only C should be in the new credit note
        assert cn3.amount_gross == Decimal("50.00")
        assert list(cn3.payments.values_list("id", flat=True)) == [payments[2].id]

        # Invoice should now be fully cancelled
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_superset_retry_all_already_credited_returns_none(
        self,
        mock_email: t.Any,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Webhook retry where all payments are already credited should return None."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        p1 = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_allcredited",
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="Guest 1",
        )
        p2 = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_allcredited",
            amount=Decimal("100.00"),
            net_amount=Decimal("81.97"),
            vat_amount=Decimal("18.03"),
            buyer_billing_snapshot=_default_billing_snapshot(),
            guest_name="Guest 2",
        )
        invoice = generate_attendee_invoice("cs_allcredited")
        assert invoice is not None

        # Credit both individually
        generate_attendee_credit_note("cs_allcredited", [p1.id])
        generate_attendee_credit_note("cs_allcredited", [p2.id])

        # Retry with both — all already credited, should return None
        result = generate_attendee_credit_note("cs_allcredited", [p1.id, p2.id])
        assert result is None
