"""Buyer-side audit trail for credited invoices (#1012).

- GET /dashboard/invoices -- ISSUED and CANCELLED invoices, with their credit notes
- GET /dashboard/invoices/{id}/download -- CANCELLED invoices stay downloadable
- GET /dashboard/credit-notes/{id}/download -- the buyer's own credit notes only
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
from events.tests.test_controllers.test_attendee_invoice_endpoints import (
    MOCK_RENDER_PDF,
    _create_draft_invoice,
    _create_issued_invoice,
)

pytestmark = pytest.mark.django_db


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _credited_invoice(org: Organization, event: Event, user: RevelUser, suffix: str) -> AttendeeInvoice:
    """A fully refunded invoice: CANCELLED, with one credit note for the full amount."""
    invoice = _create_issued_invoice(org, event, user, suffix=suffix)
    AttendeeInvoiceCreditNote.objects.create(
        invoice=invoice,
        credit_note_number=f"CN-{suffix}",
        amount_gross=Decimal("100.00"),
        amount_net=Decimal("81.97"),
        amount_vat=Decimal("18.03"),
        issued_at=timezone.now(),
    )
    invoice.status = AttendeeInvoice.InvoiceStatus.CANCELLED
    invoice.save(update_fields=["status"])
    return invoice


def test_list_includes_cancelled_invoices_with_credit_notes(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    _create_draft_invoice(organization, event, member_user, suffix="cn_draft")
    issued = _create_issued_invoice(organization, event, member_user, suffix="cn_issued")
    credited = _credited_invoice(organization, event, member_user, suffix="cn_credited")

    response = _client(member_user).get(reverse("api:dashboard_invoices"))

    assert response.status_code == 200, response.content
    by_id = {row["id"]: row for row in response.json()["results"]}
    assert set(by_id) == {str(issued.id), str(credited.id)}  # the draft stays hidden
    assert by_id[str(issued.id)]["credit_notes"] == []
    [note] = by_id[str(credited.id)]["credit_notes"]
    assert by_id[str(credited.id)]["status"] == "cancelled"
    assert note["credit_note_number"] == "CN-cn_credited"
    assert note["invoice_number"] == credited.invoice_number


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
def test_cancelled_invoice_and_its_credit_note_are_downloadable(
    mock_pdf: t.Any, organization: Organization, event: Event, member_user: RevelUser
) -> None:
    credited = _credited_invoice(organization, event, member_user, suffix="cn_dl")
    note = credited.credit_notes.get()
    client = _client(member_user)

    invoice_response = client.get(reverse("api:dashboard_invoice_download", kwargs={"invoice_id": str(credited.id)}))
    note_response = client.get(reverse("api:dashboard_credit_note_download", kwargs={"credit_note_id": str(note.id)}))

    assert invoice_response.status_code == 200, invoice_response.content
    assert note_response.status_code == 200, note_response.content
    assert "download_url" in note_response.json()


def test_cannot_download_another_users_credit_note(
    organization: Organization, event: Event, member_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    credited = _credited_invoice(organization, event, organization_owner_user, suffix="cn_other")
    note = credited.credit_notes.get()

    response = _client(member_user).get(
        reverse("api:dashboard_credit_note_download", kwargs={"credit_note_id": str(note.id)})
    )

    assert response.status_code == 404
