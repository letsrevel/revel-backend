"""Tests for the ticket seeder's payment creation (#550)."""

import io
import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.management.commands.seeder.config import SeederConfig
from events.management.commands.seeder.state import SeederState
from events.management.commands.seeder.tickets import TicketSeeder
from events.models import Event, Organization, Payment, Ticket, TicketTier


@pytest.fixture
def online_ticket(db: t.Any) -> Ticket:
    """An ACTIVE ticket on an ONLINE tier with no payment yet."""
    user = RevelUser.objects.create_user(username="buyer", email="buyer@example.com", password="x")
    org = Organization.objects.create(name="Org", slug="org", owner=user)
    now = timezone.now()
    event = Event.objects.create(
        organization=org,
        name="Event",
        slug="event",
        start=now,
        end=now + timedelta(hours=2),
    )
    tier = TicketTier.objects.create(
        event=event,
        name="GA",
        price=Decimal("45.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        quantity_sold=1,
    )
    return Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name="Buyer"
    )


def _run_payments(weights: dict[str, float]) -> TicketSeeder:
    """Run only the _create_payments pass with the given status weights."""
    config = SeederConfig(seed=1234)
    config.payment_status_weights = weights
    seeder = TicketSeeder(config=config, state=SeederState(), stdout=io.StringIO())
    seeder._create_payments()
    return seeder


@pytest.mark.django_db
def test_refunded_payment_is_internally_consistent(online_ticket: Ticket) -> None:
    """A seeded REFUNDED payment records the refund and cancels the ticket (#550)."""
    _run_payments({"succeeded": 0.0, "pending": 0.0, "failed": 0.0, "refunded": 1.0})

    payment = Payment.objects.get(ticket=online_ticket)
    assert payment.status == Payment.PaymentStatus.REFUNDED
    assert payment.refund_status == Payment.RefundStatus.SUCCEEDED
    assert payment.refund_amount is not None and payment.refund_amount > 0
    assert payment.refunded_at is not None
    assert payment.stripe_refund_id.startswith("re_test_")

    online_ticket.refresh_from_db()
    assert online_ticket.status == Ticket.TicketStatus.CANCELLED
    assert online_ticket.cancelled_at is not None

    online_ticket.tier.refresh_from_db()
    assert online_ticket.tier.quantity_sold == 0  # decremented from 1


@pytest.mark.django_db
def test_succeeded_payment_leaves_ticket_active(online_ticket: Ticket) -> None:
    """A non-refunded payment must not touch the ticket or refund fields."""
    _run_payments({"succeeded": 1.0, "pending": 0.0, "failed": 0.0, "refunded": 0.0})

    payment = Payment.objects.get(ticket=online_ticket)
    assert payment.status == Payment.PaymentStatus.SUCCEEDED
    assert payment.refund_status is None
    assert payment.refund_amount is None

    online_ticket.refresh_from_db()
    assert online_ticket.status == Ticket.TicketStatus.ACTIVE
