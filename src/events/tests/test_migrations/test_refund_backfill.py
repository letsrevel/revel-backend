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
