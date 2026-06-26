"""Tests for the EmailDeliveryAdminMixin retry action on financial-document admins (#618)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages.storage.fallback import FallbackStorage
from django.utils import timezone

from accounts.models import RevelUser
from events.admin.attendee_invoice import AttendeeInvoiceAdmin
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice

pytestmark = pytest.mark.django_db


def _admin() -> AttendeeInvoiceAdmin:
    return t.cast(AttendeeInvoiceAdmin, site._registry[AttendeeInvoice])


def _request_with_messages(rf: t.Any, user: RevelUser) -> t.Any:
    request = rf.post("/")
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _undeliverable_invoice(org: Organization, event: Event, user: RevelUser) -> AttendeeInvoice:
    inv = AttendeeInvoice.objects.create(
        organization=org,
        event=event,
        user=user,
        stripe_session_id="cs_retry_1",
        invoice_number="RETRY-000001",
        status=AttendeeInvoice.InvoiceStatus.ISSUED,
        issued_at=timezone.now(),
        total_gross=Decimal("100.00"),
        total_net=Decimal("81.97"),
        total_vat=Decimal("18.03"),
        vat_rate=Decimal("22.00"),
        currency="EUR",
        line_items=[],
        seller_name="ACME SRL",
        seller_email="billing@acme.it",
        buyer_name="Buyer GmbH",
        buyer_email="buyer@example.de",
    )
    inv.mark_email_undeliverable(AttendeeInvoice.DeliveryFailureReason.NO_RECIPIENT)
    return inv


def test_retry_clears_terminal_state_and_redispatches(
    rf: t.Any, organization: Organization, event: Event, member_user: RevelUser, superuser: RevelUser
) -> None:
    """The retry action nulls the undeliverable flag and re-dispatches the delivery task."""
    inv = _undeliverable_invoice(organization, event, member_user)
    request = _request_with_messages(rf, superuser)

    with patch("events.tasks.invoicing.deliver_attendee_invoice_task.delay") as mock_delay:
        _admin().retry_delivery(request, AttendeeInvoice.objects.filter(pk=inv.pk))

    inv.refresh_from_db()
    assert inv.email_delivery_failed_at is None
    assert inv.email_delivery_error == ""
    mock_delay.assert_called_once_with(str(inv.id))


def test_retry_ignores_documents_not_flagged_undeliverable(
    rf: t.Any, organization: Organization, event: Event, member_user: RevelUser, superuser: RevelUser
) -> None:
    """A document with no terminal-failure flag is left untouched and not re-dispatched."""
    inv = _undeliverable_invoice(organization, event, member_user)
    inv.email_delivery_failed_at = None
    inv.email_delivery_error = ""
    inv.save(update_fields=["email_delivery_failed_at", "email_delivery_error"])
    request = _request_with_messages(rf, superuser)

    with patch("events.tasks.invoicing.deliver_attendee_invoice_task.delay") as mock_delay:
        _admin().retry_delivery(request, AttendeeInvoice.objects.filter(pk=inv.pk))

    mock_delay.assert_not_called()
