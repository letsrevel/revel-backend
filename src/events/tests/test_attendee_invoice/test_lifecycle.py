"""Attendee-invoice draft lifecycle tests.

Tests cover:
- update_draft_invoice() -- editable fields, disallowed fields, PDF invalidation
- issue_draft_invoice() -- DRAFT -> ISSUED transition
- delete_draft_invoice() -- draft deletion and issued rejection

Generation tests live in test_generation.py.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_draft_service import (
    DERIVED_TOTAL_FIELDS,
    delete_draft_invoice,
    issue_draft_invoice,
    update_draft_invoice,
)
from events.service.attendee_invoice_service import (
    generate_attendee_invoice,
)
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_draft,
    _create_issued,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# update_draft_invoice
# ---------------------------------------------------------------------------


class TestUpdateDraftInvoice:
    """Test editing draft invoices."""

    def test_can_edit_buyer_name(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        """Editing buyer_name on a draft should succeed."""
        inv = _create_draft(organization, event, member_user)
        assert update_draft_invoice(inv, {"buyer_name": "New Name"}).buyer_name == "New Name"

    @pytest.mark.parametrize("field", DERIVED_TOTAL_FIELDS)
    def test_cannot_edit_derived_total(
        self, field: str, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Regression (#911): a header total cannot be stated independently of the lines.

        Setting ``total_gross`` alone used to succeed, leaving a header claiming
        one amount above line items summing to another -- a contradiction the
        API returns in a single response now that ``vat_breakdown`` is exposed
        (#910), and that issuance then turned into a delivered PDF.
        """
        inv = _create_draft(organization, event, member_user)
        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(inv, {field: Decimal("200.00")})
        assert exc_info.value.status_code == 422
        assert field in str(exc_info.value)

    def test_editing_line_items_recomputes_totals(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Rewriting the lines rewrites the header they summarize (#911)."""
        inv = _create_draft(organization, event, member_user)
        updated = update_draft_invoice(
            inv,
            {
                "line_items": [
                    {
                        "description": "Ticket A",
                        "unit_price_gross": Decimal("50.00"),
                        "discount_amount": Decimal("5.00"),
                        "net_amount": Decimal("40.98"),
                        "vat_amount": Decimal("9.02"),
                        "vat_rate": Decimal("22.00"),
                    },
                    {
                        "description": "Ticket B",
                        "unit_price_gross": Decimal("30.00"),
                        "discount_amount": Decimal("0.00"),
                        "net_amount": Decimal("27.27"),
                        "vat_amount": Decimal("2.73"),
                        "vat_rate": Decimal("10.00"),
                    },
                ]
            },
        )
        updated.refresh_from_db()
        assert updated.total_gross == Decimal("80.00")
        assert updated.total_net == Decimal("68.25")
        assert updated.total_vat == Decimal("11.75")
        assert updated.discount_amount_total == Decimal("5.00")
        # The scalar header rate follows generation's convention: the first line's.
        assert updated.vat_rate == Decimal("22.00")

    def test_recomputed_totals_reconcile_with_vat_breakdown(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """The invariant ``InvoiceVatBucketDict`` promises holds after an edit (#911)."""
        inv = _create_draft(organization, event, member_user)
        updated = update_draft_invoice(
            inv,
            {
                "line_items": [
                    {
                        "description": "Standard",
                        "unit_price_gross": Decimal("111.00"),
                        "discount_amount": Decimal("0.00"),
                        "net_amount": Decimal("91.74"),
                        "vat_amount": Decimal("19.26"),
                        "vat_rate": Decimal("21.00"),
                    },
                    {
                        "description": "Reduced",
                        "unit_price_gross": Decimal("22.00"),
                        "discount_amount": Decimal("0.00"),
                        "net_amount": Decimal("20.00"),
                        "vat_amount": Decimal("2.00"),
                        "vat_rate": Decimal("10.00"),
                    },
                ]
            },
        )
        buckets = updated.vat_breakdown
        assert updated.has_mixed_vat_rates
        assert sum(b["gross_amount"] for b in buckets) == updated.total_gross
        assert sum(b["net_amount"] for b in buckets) == updated.total_net
        assert sum(b["vat_amount"] for b in buckets) == updated.total_vat

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
            "currency": "USD",
            "reverse_charge": True,
            "discount_code_text": "SALE10",
            "line_items": [{"description": "Test"}],
        }
        update_draft_invoice(inv, data)  # Should not raise

    def test_edit_rejects_an_invoice_issued_since_it_was_fetched(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """The DRAFT check must judge the locked row, not the caller's snapshot (#911 review).

        The controller fetches the invoice, then hands it here. Unlocked, a
        concurrent issue landing in that window left this function checking a
        stale DRAFT and editing an already-issued legal document.
        """
        inv = _create_draft(organization, event, member_user)
        stale = AttendeeInvoice.objects.get(pk=inv.pk)  # what the controller handed us
        AttendeeInvoice.objects.filter(pk=inv.pk).update(status=AttendeeInvoice.InvoiceStatus.ISSUED)

        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(stale, {"buyer_name": "Too late"})

        assert exc_info.value.status_code == 409
        inv.refresh_from_db()
        assert inv.buyer_name == "Original Buyer"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_issue_reconciles_the_locked_row_not_the_callers_snapshot(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Reconciliation must judge the committed row (#911 review).

        Unlocked, a PATCH landing after the controller's fetch was validated in
        absentia: the guard passed on the stale copy and the PDF rendered from it,
        so the buyer received a document contradicting the stored invoice.
        """
        inv = _create_draft(organization, event, member_user)
        stale = AttendeeInvoice.objects.get(pk=inv.pk)
        AttendeeInvoice.objects.filter(pk=inv.pk).update(total_gross=Decimal("999.00"))

        with pytest.raises(HttpError) as exc_info:
            issue_draft_invoice(stale)

        assert exc_info.value.status_code == 422
        inv.refresh_from_db()
        assert inv.status == AttendeeInvoice.InvoiceStatus.DRAFT
        mock_pdf.assert_not_called()

    @pytest.mark.parametrize(
        "field",
        ["buyer_vat_id", "buyer_vat_country", "buyer_address", "buyer_email", "discount_code_text"],
    )
    def test_null_blankable_field_coerced_to_empty_string(
        self, field: str, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Regression (#700): null on a blankable (blank=True) column is coerced to "" (NOT NULL)."""
        inv = _create_draft(organization, event, member_user)
        updated = update_draft_invoice(inv, {field: None})
        assert getattr(updated, field) == ""
        updated.refresh_from_db()
        assert getattr(updated, field) == ""

    @pytest.mark.parametrize("field", ["buyer_name", "currency"])
    def test_null_required_field_rejected(
        self, field: str, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """Required columns lack blank=True, so an explicit null is rejected up front (#908).

        Previously this reached ``full_clean()`` and surfaced as a Django
        ``ValidationError`` (a 400); the null guard now rejects it as a 422
        naming the offending field, before anything is written.
        """
        inv = _create_draft(organization, event, member_user)
        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(inv, {field: None})
        assert exc_info.value.status_code == 422
        assert field in str(exc_info.value)


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

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_cannot_issue_draft_whose_totals_drifted(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Regression (#911): a drifted DRAFT must not become a legal document.

        The API can no longer produce one, but a row written straight through
        the Django admin -- or one that drifted before #911 -- still can, and
        issuance is its own gate: it used to return 200 and deliver a PDF whose
        header contradicted its own line items.
        """
        inv = _create_draft(organization, event, member_user)
        AttendeeInvoice.objects.filter(pk=inv.pk).update(total_gross=Decimal("999.00"))
        inv.refresh_from_db()

        with pytest.raises(HttpError) as exc_info:
            issue_draft_invoice(inv)

        assert exc_info.value.status_code == 422
        assert "total_gross" in str(exc_info.value)
        assert "999.00" in str(exc_info.value)
        inv.refresh_from_db()
        assert inv.status == AttendeeInvoice.InvoiceStatus.DRAFT
        mock_pdf.assert_not_called()

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_issue_guard_tolerates_a_line_item_without_discount_amount(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """The guard must judge a row, not crash on it (#911 review).

        ``discount_amount`` is the one key ``vat_breakdown`` never reads, so a
        row lacking it renders fine everywhere else. The guard runs on rows
        nobody normalized, where a bare ``item["discount_amount"]`` would have
        been a 500 on exactly the malformed document it exists to catch.
        """
        inv = _create_draft(organization, event, member_user)
        AttendeeInvoice.objects.filter(pk=inv.pk).update(
            line_items=[
                {
                    "description": "legacy row",
                    "unit_price_gross": "100.00",
                    "net_amount": "81.97",
                    "vat_amount": "18.03",
                    "vat_rate": "22.00",
                }
            ]
        )
        inv.refresh_from_db()

        assert inv.vat_breakdown  # readable, as it always was
        assert issue_draft_invoice(inv).status == AttendeeInvoice.InvoiceStatus.ISSUED

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_patching_line_items_repairs_a_drifted_draft(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """The documented recovery path: restate the lines, the header follows (#911)."""
        inv = _create_draft(organization, event, member_user)
        AttendeeInvoice.objects.filter(pk=inv.pk).update(total_gross=Decimal("999.00"))
        inv.refresh_from_db()

        repaired = update_draft_invoice(inv, {"line_items": list(inv.line_items)})

        assert repaired.total_gross == Decimal("100.00")
        assert issue_draft_invoice(repaired).status == AttendeeInvoice.InvoiceStatus.ISSUED

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_reissue_of_drifted_issued_invoice_still_regenerates_pdf(
        self,
        mock_pdf: t.Any,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """The guard covers only DRAFT -> ISSUED (#911).

        Re-issuing exists to recover a failed PDF generation on an already-issued
        document; refusing it would strand a row that drifted before the guard
        landed with no PDF at all.
        """
        inv = _create_issued(organization, event, member_user)
        AttendeeInvoice.objects.filter(pk=inv.pk).update(total_gross=Decimal("999.00"))
        inv.refresh_from_db()

        assert issue_draft_invoice(inv).status == AttendeeInvoice.InvoiceStatus.ISSUED
        mock_pdf.assert_called_once()


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
