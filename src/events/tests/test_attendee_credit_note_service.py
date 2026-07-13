"""Tests for attendee credit note generation (generate_attendee_credit_note).

Split out of test_attendee_invoice_service.py to keep both files under the
1000-line limit; shares the invoice test helpers from that module.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_service import (
    generate_attendee_credit_note,
    generate_attendee_invoice,
)
from events.tests.test_attendee_invoice_service import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = pytest.mark.django_db


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
