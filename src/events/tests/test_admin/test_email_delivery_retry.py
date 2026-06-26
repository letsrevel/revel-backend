"""Tests for the EmailDeliveryAdminMixin retry action on financial-document admins (#618)."""

import typing as t
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages.storage.fallback import FallbackStorage
from django.utils import timezone

from accounts.models import RevelUser
from events.admin.attendee_invoice import AttendeeInvoiceAdmin
from events.admin.invoice import PlatformFeeInvoiceAdmin
from events.models import Event, Organization
from events.models.attendee_invoice import AttendeeInvoice
from events.models.invoice import PlatformFeeInvoice

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
    rf: t.Any,
    organization: Organization,
    event: Event,
    member_user: RevelUser,
    superuser: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """The retry action nulls the undeliverable flag and re-dispatches on commit."""
    inv = _undeliverable_invoice(organization, event, member_user)
    request = _request_with_messages(rf, superuser)

    with patch("events.tasks.invoicing.deliver_attendee_invoice_task.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
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


def _org_deleted_platform_fee_invoice(org: Organization) -> PlatformFeeInvoice:
    inv = PlatformFeeInvoice.objects.create(
        organization=org,
        invoice_number="RETRY-PF-000001",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        fee_gross=Decimal("100.00"),
        fee_net=Decimal("81.97"),
        fee_vat=Decimal("18.03"),
        fee_vat_rate=Decimal("22.00"),
        currency="EUR",
        org_name=org.name,
        org_vat_id="",
        platform_business_name="Revel",
        platform_business_address="Test",
        platform_vat_id="IT99999999999",
        status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
        issued_at=timezone.now(),
    )
    # Simulate org deletion (SET_NULL), then flag the invoice terminally undeliverable.
    PlatformFeeInvoice.objects.filter(pk=inv.pk).update(organization=None)
    inv.refresh_from_db()
    inv.mark_email_undeliverable(PlatformFeeInvoice.DeliveryFailureReason.ORG_DELETED)
    return inv


def test_retry_on_org_deleted_invoice_does_not_trip_full_clean(
    rf: t.Any,
    organization: Organization,
    superuser: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Retrying an ORG_DELETED invoice (null org FK) clears state via update() without full_clean."""
    inv = _org_deleted_platform_fee_invoice(organization)
    admin_instance = t.cast(PlatformFeeInvoiceAdmin, site._registry[PlatformFeeInvoice])
    request = _request_with_messages(rf, superuser)

    with patch("events.tasks.invoicing.send_invoice_email_task.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            admin_instance.retry_delivery(request, PlatformFeeInvoice.objects.filter(pk=inv.pk))

    inv.refresh_from_db()
    assert inv.email_delivery_failed_at is None
    assert inv.email_delivery_error == ""
    assert inv.organization_id is None  # the null org FK was not resurrected
    mock_delay.assert_called_once_with(str(inv.id))
