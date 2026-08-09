"""Tests for the 0113 backfill data migration."""

import importlib
import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from events.models import Payment, Refund, Ticket

pytestmark = pytest.mark.django_db

# The migration module name starts with a digit, so it can't be a normal import.
_migration = importlib.import_module("events.migrations.0113_backfill_refund_rows")
backfill_refunds = _migration.backfill_refunds


def test_backfill_creates_row_from_legacy_refund_block(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    refunded_at = timezone.now() - timedelta(days=30)
    ticket = ticket_factory(status=Ticket.TicketStatus.CANCELLED, cancellation_source="user")
    payment = payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        refund_amount=Decimal("40.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED,
        refunded_at=refunded_at,
        stripe_refund_id="re_legacy",
    )

    backfill_refunds(django_apps, None)

    row = Refund.objects.get(payment=payment)
    assert row.amount == Decimal("40.00")
    assert row.status == Refund.RefundStatus.SUCCEEDED
    assert row.stripe_refund_id == "re_legacy"
    assert row.source == Refund.Source.USER_CANCELLATION
    assert row.succeeded_at == refunded_at  # preserves the legacy period attribution


def test_backfill_preserves_pending_status(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    """A PENDING legacy refund block backfills to a PENDING Refund row, not just SUCCEEDED/FAILED."""
    ticket = ticket_factory(status=Ticket.TicketStatus.CANCELLED, cancellation_source="organizer")
    payment = payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        refund_amount=Decimal("40.00"),
        refund_status=Payment.RefundStatus.PENDING,
        stripe_refund_id="re_pending",
    )

    backfill_refunds(django_apps, None)

    row = Refund.objects.get(payment=payment)
    assert row.status == Refund.RefundStatus.PENDING
    assert row.source == Refund.Source.ORGANIZER_API


def test_backfill_preserves_failed_status_and_failure_reason(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    """A FAILED legacy refund block backfills with its status and failure_reason intact."""
    ticket = ticket_factory(status=Ticket.TicketStatus.CANCELLED, cancellation_source="user")
    payment = payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        refund_amount=Decimal("40.00"),
        refund_status=Payment.RefundStatus.FAILED,
        refund_failure_reason="balance_insufficient",
    )

    backfill_refunds(django_apps, None)

    row = Refund.objects.get(payment=payment)
    assert row.status == Refund.RefundStatus.FAILED
    assert row.failure_reason == "balance_insufficient"


def test_backfill_falls_back_to_payment_amount_when_refund_amount_is_none(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    """A legacy row with no recorded ``refund_amount`` backfills using the full ``payment.amount``."""
    ticket = ticket_factory(status=Ticket.TicketStatus.CANCELLED, cancellation_source="user")
    payment = payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        refund_amount=None,
        refund_status=Payment.RefundStatus.SUCCEEDED,
    )

    backfill_refunds(django_apps, None)

    row = Refund.objects.get(payment=payment)
    assert row.amount == Decimal("40.00")


def test_backfill_unmapped_cancellation_source_defaults_to_stripe_dashboard(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    """A cancellation_source with no entry in the source map (e.g. a bulk event
    cancellation) backfills as ``stripe_dashboard`` rather than raising or defaulting
    to a misleading value."""
    ticket = ticket_factory(status=Ticket.TicketStatus.CANCELLED, cancellation_source="event_cancellation")
    payment = payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        refund_amount=Decimal("40.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED,
    )

    backfill_refunds(django_apps, None)

    row = Refund.objects.get(payment=payment)
    assert row.source == Refund.Source.STRIPE_DASHBOARD
