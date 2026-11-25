"""The organizer invoice endpoints answer 404 when a draft is deleted mid-request (#1498).

The deletion is placed exactly in the race window: right after the controller's read (edit, issue), or
during the slow, unlocked PDF render (download).
"""

import typing as t
from unittest.mock import patch

import pytest
from django.shortcuts import get_object_or_404
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.tests.test_controllers.test_attendee_invoice_endpoints import MOCK_RENDER_PDF, _create_draft_invoice

pytestmark = pytest.mark.django_db

VAT_GET_OBJECT_OR_404 = "events.controllers.organization_admin.vat.get_object_or_404"


def _read_then_delete(*args: t.Any, **kwargs: t.Any) -> t.Any:
    """The controller's read succeeds; the row is deleted before the service locks it."""
    obj = get_object_or_404(*args, **kwargs)
    if isinstance(obj, AttendeeInvoice):
        AttendeeInvoice.objects.filter(pk=obj.pk).delete()
    return obj


@pytest.fixture
def draft(organization: Organization, event: Event, member_user: RevelUser) -> AttendeeInvoice:
    return _create_draft_invoice(organization, event, member_user, suffix="gone")


def _url(name: str, organization: Organization, invoice: AttendeeInvoice) -> str:
    return reverse(f"api:{name}", kwargs={"slug": organization.slug, "invoice_id": str(invoice.id)})


def test_edit_of_a_draft_deleted_mid_request_is_404(
    organization_owner_client: Client, organization: Organization, draft: AttendeeInvoice
) -> None:
    with patch(VAT_GET_OBJECT_OR_404, side_effect=_read_then_delete):
        response = organization_owner_client.patch(
            _url("update_attendee_invoice", organization, draft),
            data={"buyer_name": "New Buyer"},
            content_type="application/json",
        )

    assert (response.status_code, response.json()) == (404, {"detail": "Not found."})


def test_issue_of_a_draft_deleted_mid_request_is_404(
    organization_owner_client: Client, organization: Organization, draft: AttendeeInvoice
) -> None:
    with patch(VAT_GET_OBJECT_OR_404, side_effect=_read_then_delete):
        response = organization_owner_client.post(_url("issue_attendee_invoice", organization, draft))

    assert (response.status_code, response.json()) == (404, {"detail": "Not found."})


def test_download_of_a_draft_deleted_during_the_render_is_404(
    organization_owner_client: Client, organization: Organization, draft: AttendeeInvoice
) -> None:
    def delete_during_render(*args: t.Any, **kwargs: t.Any) -> bytes:
        AttendeeInvoice.objects.filter(pk=draft.pk).delete()
        return b"draft-pdf"

    with patch(MOCK_RENDER_PDF, side_effect=delete_during_render):
        response = organization_owner_client.get(_url("download_attendee_invoice", organization, draft))

    assert (response.status_code, response.json()) == (404, {"detail": "Invoice PDF could not be generated."})
