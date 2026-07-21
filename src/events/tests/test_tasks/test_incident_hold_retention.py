"""Tests for the incident-hold retention path in cleanup_expired_payments (#756).

A recorded ``stripe_session_total_mismatch`` marks its Payment rows with an
incident hold: the expiry sweep must retain held rows (they ARE the incident
evidence), still delete ordinary expired rows, and eventually reclaim held
rows too — either when an operator resolves the hold (clears
``incident_hold_at``) or when the bounded retention window lapses.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Payment, Ticket, TicketTier
from events.service.stripe_incidents import record_session_total_mismatch
from events.tasks import cleanup_expired_payments

pytestmark = pytest.mark.django_db


def _expired_pending_payment(tier: TicketTier, user: RevelUser, guest_name: str, session_id: str) -> Payment:
    """Create a PENDING ticket + expired PENDING payment on the given tier."""
    ticket = Ticket.objects.create(
        guest_name=guest_name, event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
    )
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=session_id,
        status=Payment.PaymentStatus.PENDING,
        expires_at=timezone.now() - timedelta(minutes=1),
        amount=tier.price,
        platform_fee=10,
    )


def _record_mismatch(payment: Payment) -> None:
    """Record a session-total mismatch implicating the given payment.

    Celery is eager in tests (see src/conftest.py), so the hold task dispatched
    by the recorder runs inline.
    """
    record_session_total_mismatch(
        call_site="webhook",
        payments=[payment],
        charged_minor_units=1100,
        recorded_minor_units=1000,
        currency="EUR",
        session_id=payment.stripe_session_id,
        payment_intent_id="pi_test",
    )


class TestIncidentHoldRetention:
    @pytest.fixture
    def user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        return revel_user_factory()

    @pytest.fixture
    def tier(self, event: Event) -> TicketTier:
        tier, _ = TicketTier.objects.get_or_create(event=event, name="Paid Tier", price=Decimal("10.00"))
        return tier

    def test_mismatch_implicated_payment_survives_sweep(self, tier: TicketTier, user: RevelUser) -> None:
        """The PENDING rows of a recorded mismatch are the incident evidence: the sweep must retain them."""
        payment = _expired_pending_payment(tier, user, "Held Guest", "sess_mismatch")
        tier.quantity_sold = 1
        tier.save()

        _record_mismatch(payment)

        result = cleanup_expired_payments()

        assert result == 0
        assert Payment.objects.filter(pk=payment.pk).exists()
        assert Ticket.objects.filter(pk=payment.ticket_id).exists()
        tier.refresh_from_db()
        # Capacity stays accounted while the row is held: releasing it now and
        # again at reclaim time would double-decrement the tier.
        assert tier.quantity_sold == 1

    def test_ordinary_expired_payment_still_deleted_next_to_held_one(self, tier: TicketTier, user: RevelUser) -> None:
        """The hold is a targeted exemption, not a sweep outage: unimplicated rows still expire."""
        held = _expired_pending_payment(tier, user, "Held Guest", "sess_mismatch")
        ordinary = _expired_pending_payment(tier, user, "Ordinary Guest", "sess_ordinary")
        tier.quantity_sold = 2
        tier.save()

        _record_mismatch(held)

        result = cleanup_expired_payments()

        assert result == 1
        assert Payment.objects.filter(pk=held.pk).exists()
        assert not Payment.objects.filter(pk=ordinary.pk).exists()
        assert not Ticket.objects.filter(pk=ordinary.ticket_id).exists()
        tier.refresh_from_db()
        assert tier.quantity_sold == 1  # Only the ordinary row's unit released.

    def test_lapsed_hold_is_reclaimed_by_the_sweep(self, tier: TicketTier, user: RevelUser) -> None:
        """A held row is not immortal: once the retention window lapses, the normal reclaim applies."""
        from events.tasks.payments import INCIDENT_HOLD_RETENTION

        payment = _expired_pending_payment(tier, user, "Held Guest", "sess_mismatch")
        Payment.objects.filter(pk=payment.pk).update(
            incident_hold_at=timezone.now() - INCIDENT_HOLD_RETENTION - timedelta(hours=1)
        )
        tier.quantity_sold = 1
        tier.save()

        result = cleanup_expired_payments()

        assert result == 1
        assert not Payment.objects.filter(pk=payment.pk).exists()
        assert not Ticket.objects.filter(pk=payment.ticket_id).exists()
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_unlapsed_hold_survives_repeated_sweeps(self, tier: TicketTier, user: RevelUser) -> None:
        """A hold inside the retention window keeps surviving sweeps, not just the first one."""
        from events.tasks.payments import INCIDENT_HOLD_RETENTION

        payment = _expired_pending_payment(tier, user, "Held Guest", "sess_mismatch")
        # One hour short of lapsing — still inside the window.
        Payment.objects.filter(pk=payment.pk).update(
            incident_hold_at=timezone.now() - INCIDENT_HOLD_RETENTION + timedelta(hours=1)
        )

        assert cleanup_expired_payments() == 0
        assert cleanup_expired_payments() == 0
        assert Payment.objects.filter(pk=payment.pk).exists()

    def test_resolved_hold_is_reclaimed_on_next_sweep(self, tier: TicketTier, user: RevelUser) -> None:
        """Clearing incident_hold_at (the operator resolution path) releases the row to the sweep."""
        payment = _expired_pending_payment(tier, user, "Held Guest", "sess_mismatch")
        tier.quantity_sold = 1
        tier.save()

        _record_mismatch(payment)
        assert cleanup_expired_payments() == 0  # Retained while held.

        # Operator resolves the incident (refund issued / tickets re-issued) and
        # clears the hold — e.g. via the Payment admin.
        Payment.objects.filter(pk=payment.pk).update(incident_hold_at=None)

        result = cleanup_expired_payments()

        assert result == 1
        assert not Payment.objects.filter(pk=payment.pk).exists()
        assert not Ticket.objects.filter(pk=payment.ticket_id).exists()
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_hold_is_idempotent_first_detection_wins(self, tier: TicketTier, user: RevelUser) -> None:
        """A redelivered webhook re-records the mismatch; the retention clock must not restart."""
        from events.tasks.payments import hold_mismatch_payments

        payment = _expired_pending_payment(tier, user, "Held Guest", "sess_mismatch")

        hold_mismatch_payments([str(payment.pk)])
        payment.refresh_from_db()
        first_hold = payment.incident_hold_at
        assert first_hold is not None

        hold_mismatch_payments([str(payment.pk)])
        payment.refresh_from_db()
        assert payment.incident_hold_at == first_hold

    def test_recording_mismatch_with_no_payments_dispatches_no_hold(self) -> None:
        """The paid-session-without-payments shape has no rows to hold; the recorder must not dispatch."""
        with patch("events.tasks.payments.hold_mismatch_payments.delay") as mock_delay:
            record_session_total_mismatch(
                call_site="preflight",
                payments=[],
                charged_minor_units=1100,
                recorded_minor_units=0,
                currency="EUR",
            )
        mock_delay.assert_not_called()
