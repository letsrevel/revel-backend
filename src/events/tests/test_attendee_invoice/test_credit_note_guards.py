"""Guards of the credit-note generation (#1485, #1486).

- A refund on a payment that the invoice (or the skipped invoice) does not show gets no credit note,
  whichever caller asks for it (a refund, a duplicate webhook, a replay, the invoice task).
- A draft that was issued after the task read it is credited, not deleted.
- A draft is deleted only for a refund on a payment that it shows (#1497).
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db.models import QuerySet
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Refund, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.service.attendee_invoice_draft_service import delete_draft_invoice
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.tasks import generate_attendee_credit_note_task, generate_attendee_invoice_task
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_side_effects")]

SID = "cs_cn_guards"
BE_BUYER = {
    **_default_billing_snapshot(),
    "vat_id": "BE0123456789",
    "vat_country_code": "BE",
    "vat_id_status": "valid",
}


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
    """A Stripe refund as the ``charge.refunded`` handler leaves it."""
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
    return row


class TestRefundOfAPaymentThatIsNotOnTheInvoice:
    """#1485: two payments in one checkout, one fully refunded before the invoice."""

    def test_replay_gives_no_credit_note(self, invoicing_org: Organization, pay: t.Callable[..., Payment]) -> None:
        kept = pay()
        refunded = pay()
        row = _refund(refunded, "100.00")
        generate_attendee_invoice_task(SID)
        invoice = AttendeeInvoice.objects.get(stripe_session_id=SID)
        assert {line.get("payment_id") for line in invoice.line_items} == {str(kept.id)}

        # A replayed ``charge.refunded`` sends each succeeded refund of the payment intent.
        generate_attendee_credit_note_task(SID, [str(refunded.id)], [str(row.id)])

        assert not AttendeeInvoiceCreditNote.objects.exists()
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED

    def test_replay_credits_only_the_refund_of_the_invoiced_payment(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        kept = pay()
        refunded = pay()
        off_invoice = _refund(refunded, "100.00")
        generate_attendee_invoice_task(SID)
        on_invoice = _refund(kept, "30.00")

        note = generate_attendee_credit_note(SID, [kept.id, refunded.id], refund_ids=[off_invoice.id, on_invoice.id])

        assert note is not None
        assert note.amount_gross == Decimal("30.00")
        assert list(note.refunds.all()) == [on_invoice]
        assert list(note.payments.all()) == [kept]
        assert note.invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED
        # The same replay again finds the note of the invoiced refund and makes no new one.
        again = generate_attendee_credit_note(SID, [kept.id, refunded.id], refund_ids=[off_invoice.id, on_invoice.id])
        assert again == note
        assert AttendeeInvoiceCreditNote.objects.count() == 1

    def test_payment_keyed_request_gives_no_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        """A message without refund ids (queued before they existed) follows the same rule."""
        pay()
        refunded = pay()
        _refund(refunded, "100.00")
        generate_attendee_invoice_task(SID)

        assert generate_attendee_credit_note(SID, [refunded.id]) is None

        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_invoice_whose_lines_have_no_payment_ids_is_credited_as_before(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        payment = pay()
        invoice = generate_attendee_invoice(SID)
        assert invoice is not None
        for line in invoice.line_items:
            del line["payment_id"]
        invoice.save(update_fields=["line_items"])
        row = _refund(payment, "100.00")

        note = generate_attendee_credit_note(SID, [payment.id], refund_ids=[row.id])

        assert note is not None
        assert note.amount_gross == Decimal("100.00")

    def test_skipped_invoice_records_no_skipped_credit_note(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        invoicing_org.vat_country_code = "BE"
        invoicing_org.vat_id = "BE123456789"
        invoicing_org.save()
        kept = pay(BE_BUYER)
        refunded = pay(BE_BUYER)
        off_record = _refund(refunded, "100.00")
        generate_attendee_invoice_task(SID)
        parent = SkippedFiscalDocument.objects.get(kind=SkippedFiscalDocument.Kind.INVOICE)
        assert list(parent.payments.all()) == [kept]

        assert generate_attendee_credit_note(SID, [refunded.id], refund_ids=[off_record.id]) is None
        assert generate_attendee_credit_note(SID, [refunded.id]) is None

        assert not SkippedFiscalDocument.objects.filter(kind=SkippedFiscalDocument.Kind.CREDIT_NOTE).exists()

        # A refund on the payment that the record shows is still recorded.
        on_record = _refund(kept, "40.00")
        assert generate_attendee_credit_note(SID, [kept.id], refund_ids=[off_record.id, on_record.id]) is None
        doc = SkippedFiscalDocument.objects.get(kind=SkippedFiscalDocument.Kind.CREDIT_NOTE)
        assert list(doc.refunds.all()) == [on_record]

    def test_skipped_invoice_that_lost_all_its_payments_records_nothing(
        self, invoicing_org: Organization, pay: t.Callable[..., Payment]
    ) -> None:
        """Deleting a tier cascades to the payments; an empty record must not credit any refund."""
        invoicing_org.vat_country_code = "BE"
        invoicing_org.vat_id = "BE123456789"
        invoicing_org.save()
        kept = pay(BE_BUYER)
        refunded = pay(BE_BUYER)
        off_record = _refund(refunded, "100.00")
        generate_attendee_invoice_task(SID)
        parent = SkippedFiscalDocument.objects.get(kind=SkippedFiscalDocument.Kind.INVOICE)
        parent.payments.remove(kept)
        assert not parent.payments.exists()

        assert generate_attendee_credit_note(SID, [refunded.id], refund_ids=[off_record.id]) is None
        assert generate_attendee_credit_note(SID, [refunded.id]) is None

        assert not SkippedFiscalDocument.objects.filter(kind=SkippedFiscalDocument.Kind.CREDIT_NOTE).exists()


class TestRefundOfAPaymentThatIsNotOnTheDraft:
    """#1497: a draft that shows only the payment that was not refunded."""

    @pytest.fixture
    def draft(self, invoicing_org: Organization, pay: t.Callable[..., Payment]) -> tuple[AttendeeInvoice, Refund]:
        invoicing_org.invoicing_mode = Organization.InvoicingMode.HYBRID
        invoicing_org.save()
        kept = pay()
        refunded = pay()
        off_draft = _refund(refunded, "100.00")
        invoice = generate_attendee_invoice(SID)
        assert invoice is not None
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT
        assert {line.get("payment_id") for line in invoice.line_items} == {str(kept.id)}
        return invoice, off_draft

    def test_refund_request_keeps_the_draft(self, draft: tuple[AttendeeInvoice, Refund]) -> None:
        invoice, off_draft = draft

        assert generate_attendee_credit_note(SID, [off_draft.payment_id], refund_ids=[off_draft.id]) is None
        assert generate_attendee_credit_note(SID, [off_draft.payment_id]) is None

        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT
        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_refund_of_the_payment_on_the_draft_still_deletes_it(self, draft: tuple[AttendeeInvoice, Refund]) -> None:
        invoice, off_draft = draft
        kept = Payment.objects.get(pk=invoice.line_items[0]["payment_id"])
        on_draft = _refund(kept, "30.00")

        assert generate_attendee_credit_note(SID, [kept.id], refund_ids=[off_draft.id, on_draft.id]) is None

        assert not AttendeeInvoice.objects.filter(pk=invoice.pk).exists()


class TestDraftIssuedWhileTheTaskRuns:
    """#1486: the organizer issues the draft between the task's read and its delete."""

    @pytest.fixture
    def draft(self, invoicing_org: Organization, pay: t.Callable[..., Payment]) -> tuple[AttendeeInvoice, Payment]:
        invoicing_org.invoicing_mode = Organization.InvoicingMode.HYBRID
        invoicing_org.save()
        payment = pay()
        invoice = generate_attendee_invoice(SID)
        assert invoice is not None
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT
        return invoice, payment

    def test_task_credits_the_issued_invoice(self, draft: tuple[AttendeeInvoice, Payment]) -> None:
        invoice, payment = draft
        row = _refund(payment, "100.00")
        real_first = QuerySet.first

        def first_then_issue(queryset: QuerySet[t.Any]) -> t.Any:
            """The task's first read sees the draft; the organizer's issue commits right after it."""
            found = real_first(queryset)
            if isinstance(found, AttendeeInvoice) and found.status == AttendeeInvoice.InvoiceStatus.DRAFT:
                AttendeeInvoice.objects.filter(pk=found.pk).update(status=AttendeeInvoice.InvoiceStatus.ISSUED)
            return found

        with patch.object(QuerySet, "first", first_then_issue):
            note = generate_attendee_credit_note(SID, [payment.id], refund_ids=[row.id])

        assert note is not None
        assert note.amount_gross == Decimal("100.00")
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED

    def test_task_still_deletes_a_draft(self, draft: tuple[AttendeeInvoice, Payment]) -> None:
        invoice, payment = draft
        row = _refund(payment, "100.00")

        assert generate_attendee_credit_note(SID, [payment.id], refund_ids=[row.id]) is None

        assert not AttendeeInvoice.objects.filter(pk=invoice.pk).exists()
        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_task_stops_when_the_draft_was_deleted_at_the_same_moment(
        self, draft: tuple[AttendeeInvoice, Payment]
    ) -> None:
        invoice, payment = draft
        row = _refund(payment, "100.00")
        real_first = QuerySet.first

        def first_then_delete(queryset: QuerySet[t.Any]) -> t.Any:
            found = real_first(queryset)
            if isinstance(found, AttendeeInvoice):
                AttendeeInvoice.objects.filter(pk=found.pk).delete()
            return found

        with patch.object(QuerySet, "first", first_then_delete):
            assert generate_attendee_credit_note(SID, [payment.id], refund_ids=[row.id]) is None

        assert not AttendeeInvoiceCreditNote.objects.exists()

    def test_delete_refuses_a_draft_that_was_issued_after_it_was_read(
        self, draft: tuple[AttendeeInvoice, Payment]
    ) -> None:
        """The organizer's delete endpoint reads the invoice first; the issue commits before the delete."""
        stale, _ = draft
        AttendeeInvoice.objects.filter(pk=stale.pk).update(status=AttendeeInvoice.InvoiceStatus.ISSUED)

        with pytest.raises(HttpError) as exc_info:
            delete_draft_invoice(stale)

        assert exc_info.value.status_code == 409
        assert AttendeeInvoice.objects.filter(pk=stale.pk).exists()

    def test_delete_of_a_draft_that_is_already_gone_does_nothing(self, draft: tuple[AttendeeInvoice, Payment]) -> None:
        stale, _ = draft
        AttendeeInvoice.objects.filter(pk=stale.pk).delete()

        delete_draft_invoice(stale)
