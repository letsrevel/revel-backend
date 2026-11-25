"""A draft deleted between the controller's read and the service's row lock answers 404, not 500 (#1498).

A refund on the sale, or another organizer's delete, can remove the draft in that window. The caller
still holds the stale instance, so the services must treat "the locked row is gone" as not found.
"""

import pytest
from django.http import Http404

from accounts.models import RevelUser
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_draft_service import issue_draft_invoice, update_draft_invoice
from events.tests.test_attendee_invoice._helpers import _create_draft

pytestmark = pytest.mark.django_db


@pytest.fixture
def vanished_draft(organization: Organization, event: Event, member_user: RevelUser) -> AttendeeInvoice:
    """A draft instance whose row was deleted after it was read."""
    invoice = _create_draft(organization, event, member_user)
    AttendeeInvoice.objects.filter(pk=invoice.pk).delete()
    return invoice


def test_edit_of_a_deleted_draft_is_not_found(vanished_draft: AttendeeInvoice) -> None:
    with pytest.raises(Http404):
        update_draft_invoice(vanished_draft, {"buyer_name": "New Buyer"})

    # save() on a stale instance whose row is gone would INSERT it again: the draft must stay deleted.
    assert not AttendeeInvoice.objects.filter(pk=vanished_draft.pk).exists()


def test_issue_of_a_deleted_draft_is_not_found(vanished_draft: AttendeeInvoice) -> None:
    with pytest.raises(Http404):
        issue_draft_invoice(vanished_draft)

    # save() on a stale instance whose row is gone would INSERT it again: the draft must stay deleted.
    assert not AttendeeInvoice.objects.filter(pk=vanished_draft.pk).exists()
