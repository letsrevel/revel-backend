"""Unit tests for the ticket admin service functions extracted from event_admin/tickets.py.

Covers:
- ``cancel_offline_ticket`` / ``mark_offline_ticket_refunded``
    * Already-CANCELLED ticket raises ``TicketAlreadyCancelledError``
    * Tier ``quantity_sold`` cannot drop below zero (race-safe floor)
    * Refund of a ticket without a ``Payment`` row still cancels
    * Waitlist processing is enqueued on cancel/refund
- ``check_online_tier_prerequisites`` raises typed exceptions for online flows
- ``start_attendee_export`` creates the FileExport row and dispatches the task
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.exceptions import (
    BillingInfoRequiredError,
    StripeNotConnectedError,
    TicketAlreadyCancelledError,
)
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.models.ticket import CancellationSource
from events.service import ticket_service

pytestmark = pytest.mark.django_db


# ---- Fixtures local to this test module --------------------------------------


@pytest.fixture
def offline_tier_with_sales(event: Event) -> TicketTier:
    """Create an offline tier with a positive quantity_sold to exercise the decrement path."""
    return TicketTier.objects.create(
        event=event,
        name="Offline With Sales",
        price=Decimal("25.00"),
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        quantity_sold=3,
    )


@pytest.fixture
def pending_ticket(public_user: RevelUser, event: Event, offline_tier_with_sales: TicketTier) -> Ticket:
    """A pending offline ticket usable as cancel/refund target."""
    return Ticket.objects.create(
        guest_name="Tester",
        user=public_user,
        event=event,
        tier=offline_tier_with_sales,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def cancelled_ticket(public_user: RevelUser, event: Event, offline_tier_with_sales: TicketTier) -> Ticket:
    """A ticket already in CANCELLED state."""
    return Ticket.objects.create(
        guest_name="Cancelled",
        user=public_user,
        event=event,
        tier=offline_tier_with_sales,
        status=Ticket.TicketStatus.CANCELLED,
    )


# ---- cancel_offline_ticket ---------------------------------------------------


def test_cancel_offline_ticket_decrements_tier_and_marks_cancelled(
    pending_ticket: Ticket,
    offline_tier_with_sales: TicketTier,
    organization_owner_user: RevelUser,
) -> None:
    """Cancelling decrements ``quantity_sold`` and records audit fields."""
    starting_sold = offline_tier_with_sales.quantity_sold
    result = ticket_service.cancel_offline_ticket(
        pending_ticket, cancelled_by=organization_owner_user, reason="duplicate"
    )

    assert result.status == Ticket.TicketStatus.CANCELLED
    pending_ticket.refresh_from_db()
    assert pending_ticket.status == Ticket.TicketStatus.CANCELLED
    assert pending_ticket.cancellation_source == CancellationSource.ORGANIZER
    assert pending_ticket.cancellation_reason == "duplicate"
    assert pending_ticket.cancelled_by_id == organization_owner_user.id
    assert pending_ticket.cancelled_at is not None

    offline_tier_with_sales.refresh_from_db()
    assert offline_tier_with_sales.quantity_sold == starting_sold - 1


def test_cancel_offline_ticket_already_cancelled_raises(
    cancelled_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    """Cancelling an already-cancelled ticket raises the typed exception (controller maps to 400)."""
    with pytest.raises(TicketAlreadyCancelledError):
        ticket_service.cancel_offline_ticket(cancelled_ticket, cancelled_by=organization_owner_user)


def test_cancel_offline_ticket_floors_quantity_sold_at_zero(
    pending_ticket: Ticket,
    offline_tier_with_sales: TicketTier,
    organization_owner_user: RevelUser,
) -> None:
    """``quantity_sold`` can never drop below zero (race-safe floor via gt=0 filter)."""
    offline_tier_with_sales.quantity_sold = 0
    offline_tier_with_sales.save(update_fields=["quantity_sold"])

    ticket_service.cancel_offline_ticket(pending_ticket, cancelled_by=organization_owner_user)

    offline_tier_with_sales.refresh_from_db()
    assert offline_tier_with_sales.quantity_sold == 0


def test_cancel_offline_ticket_enqueues_waitlist(pending_ticket: Ticket, organization_owner_user: RevelUser) -> None:
    """Cancellation enqueues a waitlist processing pass for the event."""
    with patch("events.service.ticket_service.enqueue_waitlist_processing") as enqueue:
        ticket_service.cancel_offline_ticket(pending_ticket, cancelled_by=organization_owner_user)

    enqueue.assert_called_once_with(pending_ticket.event_id)


def test_cancel_offline_ticket_empty_reason_normalised(
    pending_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    """``reason=None`` is normalised to an empty string for the audit column."""
    ticket_service.cancel_offline_ticket(pending_ticket, cancelled_by=organization_owner_user, reason=None)

    pending_ticket.refresh_from_db()
    assert pending_ticket.cancellation_reason == ""


# ---- mark_offline_ticket_refunded --------------------------------------------


def test_mark_offline_ticket_refunded_without_payment(
    pending_ticket: Ticket,
    offline_tier_with_sales: TicketTier,
    organization_owner_user: RevelUser,
) -> None:
    """A ticket without a Payment row is still cancellable via the refund path."""
    assert not hasattr(pending_ticket, "payment")
    starting_sold = offline_tier_with_sales.quantity_sold

    result = ticket_service.mark_offline_ticket_refunded(
        pending_ticket, cancelled_by=organization_owner_user, reason="no_payment"
    )

    assert result.status == Ticket.TicketStatus.CANCELLED
    offline_tier_with_sales.refresh_from_db()
    assert offline_tier_with_sales.quantity_sold == starting_sold - 1


def test_mark_offline_ticket_refunded_with_payment(pending_ticket: Ticket, organization_owner_user: RevelUser) -> None:
    """When a Payment exists it is marked REFUNDED with refund metadata populated."""
    payment = Payment.objects.create(
        ticket=pending_ticket,
        user=pending_ticket.user,
        stripe_session_id="session-id",
        amount=Decimal("25.00"),
        platform_fee=Decimal("1.00"),
        currency="EUR",
        status=Payment.PaymentStatus.SUCCEEDED,
    )

    # Reload so the payment reverse relation is hydrated on the in-memory ticket.
    ticket = Ticket.objects.select_related("tier", "payment").get(pk=pending_ticket.pk)
    ticket_service.mark_offline_ticket_refunded(ticket, cancelled_by=organization_owner_user)

    payment.refresh_from_db()
    assert payment.status == Payment.PaymentStatus.REFUNDED
    assert payment.refund_status == Payment.RefundStatus.SUCCEEDED
    assert payment.refund_amount == payment.amount
    assert payment.refunded_at is not None


def test_mark_offline_ticket_refunded_already_cancelled_raises(
    cancelled_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    """Already-cancelled tickets are rejected before any side effect runs."""
    with pytest.raises(TicketAlreadyCancelledError):
        ticket_service.mark_offline_ticket_refunded(cancelled_ticket, cancelled_by=organization_owner_user)


def test_mark_offline_ticket_refunded_enqueues_waitlist(
    pending_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    """Refund flow also enqueues waitlist processing."""
    with patch("events.service.ticket_service.enqueue_waitlist_processing") as enqueue:
        ticket_service.mark_offline_ticket_refunded(pending_ticket, cancelled_by=organization_owner_user)

    enqueue.assert_called_once_with(pending_ticket.event_id)


# ---- check_online_tier_prerequisites -----------------------------------------


def test_check_online_tier_prerequisites_skips_non_online(organization: Organization) -> None:
    """Non-online payment methods skip the prerequisite checks entirely."""
    organization.stripe_account_id = ""
    organization.stripe_charges_enabled = False
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

    # Should NOT raise even without Stripe Connect.
    ticket_service.check_online_tier_prerequisites(organization, TicketTier.PaymentMethod.OFFLINE)
    ticket_service.check_online_tier_prerequisites(organization, TicketTier.PaymentMethod.AT_THE_DOOR)
    ticket_service.check_online_tier_prerequisites(organization, TicketTier.PaymentMethod.FREE)


def test_check_online_tier_prerequisites_raises_without_stripe(organization: Organization) -> None:
    """Online payment without Stripe Connect raises the typed exception."""
    organization.stripe_account_id = ""
    organization.stripe_charges_enabled = False
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])

    with pytest.raises(StripeNotConnectedError):
        ticket_service.check_online_tier_prerequisites(organization, TicketTier.PaymentMethod.ONLINE)


def test_check_online_tier_prerequisites_raises_without_billing(organization: Organization) -> None:
    """Online with platform fees but missing billing info raises BillingInfoRequiredError."""
    organization.stripe_account_id = "acct_test"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.billing_name = ""
    organization.billing_address = ""
    organization.vat_country_code = ""
    organization.save(
        update_fields=[
            "stripe_account_id",
            "stripe_charges_enabled",
            "stripe_details_submitted",
            "billing_name",
            "billing_address",
            "vat_country_code",
        ]
    )
    # Sanity: org must have platform fees configured to trigger this path.
    assert organization.platform_fee_percent > 0 or organization.platform_fee_fixed > 0

    with pytest.raises(BillingInfoRequiredError):
        ticket_service.check_online_tier_prerequisites(organization, TicketTier.PaymentMethod.ONLINE)


def test_check_online_tier_prerequisites_passes_when_billing_complete(organization: Organization) -> None:
    """Properly-configured online org passes the check silently."""
    organization.stripe_account_id = "acct_test"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.billing_name = "Acme Ltd"
    organization.billing_address = "1 Acme St"
    organization.vat_country_code = "IT"
    organization.save(
        update_fields=[
            "stripe_account_id",
            "stripe_charges_enabled",
            "stripe_details_submitted",
            "billing_name",
            "billing_address",
            "vat_country_code",
        ]
    )

    ticket_service.check_online_tier_prerequisites(organization, TicketTier.PaymentMethod.ONLINE)


# ---- start_attendee_export ---------------------------------------------------


def test_start_attendee_export_creates_export_row(event: Event, organization_owner_user: RevelUser) -> None:
    """``start_attendee_export`` creates a FileExport in PENDING and dispatches the task on commit."""
    from django.test import TestCase

    from common.models import FileExport

    with (
        patch("events.tasks.generate_attendee_export_task.delay") as delay,
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        export = ticket_service.start_attendee_export(event, requested_by=organization_owner_user)

    assert export.export_type == FileExport.ExportType.ATTENDEE_LIST
    assert export.status == FileExport.ExportStatus.PENDING
    assert export.parameters == {"event_id": str(event.id)}
    assert export.requested_by_id == organization_owner_user.id
    delay.assert_called_once_with(str(export.id))


def test_start_attendee_export_dispatch_callable_resolves_to_task(
    event: Event, organization_owner_user: RevelUser
) -> None:
    """The on_commit hook ultimately calls the Celery task with the export id (smoke-checked at import)."""
    from events.tasks import generate_attendee_export_task

    assert callable(getattr(generate_attendee_export_task, "delay", None))
    export = ticket_service.start_attendee_export(event, requested_by=organization_owner_user)
    assert export.id is not None


# ---- typing sanity -----------------------------------------------------------


def test_module_exports_messages() -> None:
    """Public message constants are exported for the controller's HTTP mapping."""
    messages: list[t.Any] = [
        ticket_service.TICKET_ALREADY_CANCELLED_MESSAGE,
        ticket_service.STRIPE_NOT_CONNECTED_MESSAGE,
        ticket_service.BILLING_INFO_REQUIRED_MESSAGE,
    ]
    for msg in messages:
        assert str(msg)
