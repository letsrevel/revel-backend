"""Invoices stay saveable after their event or user was deleted (SET_NULL references)."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_draft_service import issue_draft_invoice
from events.tests.test_attendee_invoice._helpers import MOCK_RENDER_PDF, _create_draft

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"):
        yield


def test_event_less_draft_can_be_issued(organization: Organization, event: Event, member_user: RevelUser) -> None:
    draft = _create_draft(organization, event, member_user)
    AttendeeInvoice.objects.filter(pk=draft.pk).update(event=None)  # what SET_NULL leaves behind
    draft.refresh_from_db()

    assert issue_draft_invoice(draft).status == AttendeeInvoice.InvoiceStatus.ISSUED


def test_invoice_of_a_deleted_user_can_still_be_saved(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = _create_draft(organization, event, member_user)
    AttendeeInvoice.objects.filter(pk=invoice.pk).update(user=None)
    invoice.refresh_from_db()

    invoice.buyer_name = "Renamed"
    invoice.save()
    invoice.refresh_from_db()
    assert (invoice.user_id, invoice.buyer_name) == (None, "Renamed")
