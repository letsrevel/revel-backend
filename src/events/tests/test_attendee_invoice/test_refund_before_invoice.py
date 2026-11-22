"""A refund that is processed before its invoice exists still gets its credit note (#1484).

The credit-note task finds no invoice and returns; the invoice task, which runs later, credits the
refunds that no credit note covers yet. Celery runs eagerly in tests, so the chained tasks run inline.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Refund, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.tasks import generate_attendee_invoice_task
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_side_effects")]

SID = "cs_refund_first"


@pytest.fixture
def _no_side_effects() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"), patch(MOCK_SEND_EMAIL):
        yield


@pytest.fixture
def invoicing_org(organization: Organization) -> Organization:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return org


@pytest.fixture
def pay(event: Event, event_ticket_tier: TicketTier, member_user: RevelUser) -> t.Callable[..., Payment]:
    def make(snapshot: dict[str, t.Any] | None = None) -> Payment:
        return _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id=SID,
            buyer_billing_snapshot=snapshot or _default_billing_snapshot(),
        )

    return make


def _refund(payment: Payment, amount: str) -> Refund:
    """A Stripe refund as the ``charge.refunded`` handler leaves it, and its credit-note attempt."""
    row = Refund.objects.create(
        payment=payment,
        amount=Decimal(amount),
        currency="EUR",
        status=Refund.RefundStatus.SUCCEEDED,
        source=Refund.Source.ORGANIZER_API,
    )
    if row.amount >= payment.amount:
        payment.status = Payment.PaymentStatus.REFUNDED
    payment.refund_status = Payment.RefundStatus.SUCCEEDED
    payment.refund_amount = row.amount
    payment.save(update_fields=["status", "refund_status", "refund_amount"])
    # The credit-note task runs first and finds no invoice yet.
    assert generate_attendee_credit_note(SID, [payment.id], refund_ids=[row.id]) is None
    return row


class TestRefundBeforeInvoice:
    def test_partial_refund_before_invoice_gets_a_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        payment = pay()
        row = _refund(payment, "30.00")

        generate_attendee_invoice_task(SID)

        invoice = AttendeeInvoice.objects.get(stripe_session_id=SID)
        assert invoice.total_gross == Decimal("100.00")
        cn = AttendeeInvoiceCreditNote.objects.get(invoice=invoice)
        assert cn.amount_gross == Decimal("30.00")
        assert list(cn.refunds.all()) == [row]
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED

    def test_invoice_that_contains_a_refunded_payment_gets_a_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        """The concurrent order: the invoice read the payment before the refund committed."""
        payment = pay()
        invoice = generate_attendee_invoice(SID)
        assert invoice is not None
        # Simulate the refund whose credit-note attempt ran while the invoice was not committed.
        row = Refund.objects.create(
            payment=payment,
            amount=payment.amount,
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )

        generate_attendee_invoice_task(SID)  # a retry or replay of the invoice task

        cn = AttendeeInvoiceCreditNote.objects.get(invoice=invoice)
        assert list(cn.refunds.all()) == [row]
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED

    def test_full_refund_of_a_payment_left_off_the_invoice_gets_no_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        kept = pay()
        refunded = pay()
        _refund(refunded, "100.00")

        generate_attendee_invoice_task(SID)

        invoice = AttendeeInvoice.objects.get(stripe_session_id=SID)
        assert {line.get("payment_id") for line in invoice.line_items} == {str(kept.id)}
        assert invoice.total_gross == Decimal("100.00")
        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_running_the_invoice_task_twice_gives_one_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        _refund(pay(), "30.00")

        generate_attendee_invoice_task(SID)
        generate_attendee_invoice_task(SID)

        assert AttendeeInvoiceCreditNote.objects.count() == 1

    def test_no_refund_gives_no_credit_note(self, invoicing_org: Organization, pay: t.Callable[..., Payment]) -> None:
        pay()

        generate_attendee_invoice_task(SID)

        assert AttendeeInvoice.objects.filter(stripe_session_id=SID).exists()
        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_refund_before_a_draft_invoice_deletes_the_draft(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        """Same rule as a refund that arrives after the draft: the draft is deleted, not credited."""
        invoicing_org.invoicing_mode = Organization.InvoicingMode.HYBRID
        invoicing_org.save()
        _refund(pay(), "30.00")

        generate_attendee_invoice_task(SID)

        assert not AttendeeInvoice.objects.filter(stripe_session_id=SID).exists()
        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_refund_before_a_skipped_invoice_records_a_skipped_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        invoicing_org.vat_country_code = "BE"
        invoicing_org.vat_id = "BE123456789"
        invoicing_org.save()
        snapshot = {
            **_default_billing_snapshot(),
            "vat_id": "BE0123456789",
            "vat_country_code": "BE",
            "vat_id_status": "valid",
        }
        payment = pay(snapshot)
        row = _refund(payment, "40.00")

        generate_attendee_invoice_task(SID)

        parent = SkippedFiscalDocument.objects.get(kind=SkippedFiscalDocument.Kind.INVOICE)
        doc = SkippedFiscalDocument.objects.get(kind=SkippedFiscalDocument.Kind.CREDIT_NOTE)
        assert doc.parent == parent
        assert list(doc.refunds.all()) == [row]

        generate_attendee_invoice_task(SID)
        assert SkippedFiscalDocument.objects.filter(kind=SkippedFiscalDocument.Kind.CREDIT_NOTE).count() == 1

    def test_refund_without_invoice_or_skip_dispatches_nothing(
        self, organization: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        """Invoicing is off: no invoice and no skip, so there is nothing to credit."""
        _refund(pay(), "30.00")

        with patch("events.tasks.invoicing.generate_attendee_credit_note_task.delay") as delay:
            generate_attendee_invoice_task(SID)

        delay.assert_not_called()
