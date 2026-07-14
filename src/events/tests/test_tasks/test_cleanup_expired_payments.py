"""Tests for the cleanup_expired_payments Celery task (split from test_misc.py for file length)."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)
from events.tasks import (
    cleanup_expired_payments,
)

pytestmark = pytest.mark.django_db


class TestCleanupExpiredPayments:
    @pytest.fixture
    def user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        return revel_user_factory()

    @pytest.fixture
    def another_organization(self, user: RevelUser) -> Organization:
        return Organization.objects.create(name="Another Org", slug="another-org", owner=user)

    @pytest.fixture
    def another_event(self, another_organization: Organization, next_week: datetime) -> Event:
        return Event.objects.create(organization=another_organization, name="Another Event", start=next_week)

    @pytest.fixture
    def tier(self, event: Event) -> TicketTier:
        tier, _ = TicketTier.objects.get_or_create(event=event, name="Paid Tier", price=Decimal("10.00"))
        return tier

    @pytest.fixture
    def another_tier(self, another_event: Event) -> TicketTier:
        tier, _ = TicketTier.objects.get_or_create(event=another_event, name="Another Tier", price=Decimal("20.00"))
        return tier

    def test_cleanup_no_expired_payments(self) -> None:
        """Test that the task does nothing and returns 0 when there are no expired payments."""
        result = cleanup_expired_payments()
        assert result == 0

    def test_cleanup_single_expired_payment(self, tier: TicketTier, user: RevelUser) -> None:
        """Test that a single expired payment and its ticket are deleted, and tier quantity is updated."""
        # Arrange
        ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=ticket,
            user=user,
            stripe_session_id="sess_expired",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        tier.quantity_sold = 1
        tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 0
        assert not Payment.objects.exists()
        assert not Ticket.objects.exists()

    def test_cleanup_multiple_expired_payments(
        self, tier: TicketTier, another_tier: TicketTier, user: RevelUser
    ) -> None:
        """Test cleanup of multiple payments across different tiers."""
        # Arrange
        # Payment 1
        ticket1 = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=ticket1,
            user=user,
            stripe_session_id="sess_expired1",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        # Payment 2
        ticket2 = Ticket.objects.create(
            guest_name="Test Guest",
            event=another_tier.event,
            tier=another_tier,
            user=user,
            status=Ticket.TicketStatus.PENDING,
        )
        Payment.objects.create(
            ticket=ticket2,
            user=user,
            stripe_session_id="sess_expired2",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=another_tier.price,
            platform_fee=10,
        )

        tier.quantity_sold = 1
        tier.save()
        another_tier.quantity_sold = 1
        another_tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 2
        tier.refresh_from_db()
        another_tier.refresh_from_db()
        assert tier.quantity_sold == 0
        assert another_tier.quantity_sold == 0
        assert not Payment.objects.exists()
        assert not Ticket.objects.exists()

    def test_cleanup_ignores_non_expired_payments(
        self, tier: TicketTier, user: RevelUser, member_user: RevelUser
    ) -> None:
        """Test that active pending payments are not affected."""
        # Arrange
        # Expired payment
        expired_ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=expired_ticket,
            user=user,
            stripe_session_id="sess_expired",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        # Active payment
        active_ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=member_user, status=Ticket.TicketStatus.PENDING
        )
        active_payment = Payment.objects.create(
            ticket=active_ticket,
            user=member_user,
            stripe_session_id="sess_active",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() + timedelta(minutes=30),
            amount=tier.price,
            platform_fee=10,
        )

        tier.quantity_sold = 2
        tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 1  # One was released
        assert Payment.objects.count() == 1
        assert Payment.objects.first() == active_payment
        assert Ticket.objects.count() == 1
        assert Ticket.objects.first() == active_ticket

    def test_cleanup_ignores_non_pending_payments(self, tier: TicketTier, user: RevelUser) -> None:
        """Test that succeeded, failed, etc. payments are not cleaned up even if expired."""
        # Arrange
        ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE
        )
        Payment.objects.create(
            ticket=ticket,
            user=user,
            stripe_session_id="sess_succeeded",
            status=Payment.PaymentStatus.SUCCEEDED,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=5,
        )

        tier.quantity_sold = 1
        tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 0
        tier.refresh_from_db()
        assert tier.quantity_sold == 1
        assert Payment.objects.count() == 1
        assert Ticket.objects.count() == 1

    def test_cleanup_floors_quantity_sold_at_zero(self, tier: TicketTier, user: RevelUser) -> None:
        """quantity_sold must never go negative, even if it's already inconsistent
        (e.g. a prior release already happened elsewhere). Defense-in-depth (#632),
        mirrors pending_checkout._release_batch_tier_capacity's Greatest floor."""
        ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=ticket,
            user=user,
            stripe_session_id="sess_expired",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        tier.quantity_sold = 0
        tier.save()

        result = cleanup_expired_payments()

        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 0
        assert not Payment.objects.exists()
        assert not Ticket.objects.exists()

    def test_cleanup_only_releases_still_pending_payment_within_batch(self, tier: TicketTier, user: RevelUser) -> None:
        """If a payment in the expired set is no longer PENDING by the time the task
        actually reclaims it (e.g. cancel_pending_checkout or the payment_intent.canceled
        webhook already reclaimed it concurrently), it must not be double-counted in the
        tier release. The concurrency guarantee itself comes from recomputing the
        delete-set + release-count INSIDE the transaction from a fresh status=PENDING
        filter, locked via select_for_update — so a concurrent reclaim on the same rows
        serializes instead of racing (#632)."""
        ticket1 = Ticket.objects.create(
            guest_name="Guest 1", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        payment1 = Payment.objects.create(
            ticket=ticket1,
            user=user,
            stripe_session_id="sess_expired1",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        ticket2 = Ticket.objects.create(
            guest_name="Guest 2", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        payment2 = Payment.objects.create(
            ticket=ticket2,
            user=user,
            stripe_session_id="sess_expired2",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        tier.quantity_sold = 2
        tier.save()

        # Simulate a concurrent reclaim (cancel_pending_checkout / the webhook) that
        # already claimed payment2 before this task's transaction locks the rows.
        Payment.objects.filter(pk=payment2.pk).update(status=Payment.PaymentStatus.SUCCEEDED)

        result = cleanup_expired_payments()

        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 1  # Only payment1's unit released.
        assert not Payment.objects.filter(pk=payment1.pk).exists()
        assert not Ticket.objects.filter(pk=ticket1.pk).exists()
        assert Payment.objects.filter(pk=payment2.pk).exists()  # Untouched.
        assert Ticket.objects.filter(pk=ticket2.pk).exists()  # Untouched.

    def test_cleanup_does_not_double_release_a_cancelled_tickets_orphaned_pending_payment(
        self, event: Event, user: RevelUser
    ) -> None:
        """A user who cancels a still-unpaid PENDING online ticket (POST /tickets/{id}/cancel)
        leaves its Payment PENDING: it never has a stripe_payment_intent_id (the checkout
        never reached Stripe), so no refund path ever touches it. cancel_ticket_by_user
        already released the tier slot at cancel time
        (cancellation_service._finalize_cancellation); cleanup_expired_payments must not
        release it again just because it's still keyed to the tier via the payment's
        ticket -- it must only count payments whose ticket is still PENDING (#632).

        A second, unrelated ACTIVE ticket on the same tier keeps quantity_sold above the
        Greatest(...,0) floor so a double-decrement is actually observable, not masked."""
        from events.service.cancellation_service import cancel_ticket_by_user

        event.start = timezone.now() + timedelta(hours=72)
        event.end = event.start + timedelta(hours=73)
        event.save(update_fields=["start", "end"])

        tier = TicketTier.objects.create(
            event=event,
            name="Cancellable Online Tier",
            price=Decimal("40.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
            allow_user_cancellation=True,
            refund_policy={"tiers": [{"hours_before_event": 48, "refund_percentage": "100"}], "flat_fee": "0"},
        )

        # Unrelated ACTIVE ticket: keeps the tier's true occupancy at 1 after the
        # reserved ticket below is cancelled, so a double-decrement would show up
        # as 0 instead of the correct 1 (not masked by the Greatest(...,0) floor).
        Ticket.objects.create(
            guest_name="Other Guest", event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE
        )

        reserved_ticket = Ticket.objects.create(
            guest_name="Reserved Guest",
            event=event,
            tier=tier,
            user=user,
            status=Ticket.TicketStatus.PENDING,
            refund_policy_snapshot=tier.refund_policy,
        )
        payment = Payment.objects.create(
            ticket=reserved_ticket,
            user=user,
            stripe_session_id="sess_reserved",
            stripe_payment_intent_id="",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        tier.quantity_sold = 2
        tier.save()

        with patch("stripe.Refund.create") as mock_refund_create:
            cancel_ticket_by_user(reserved_ticket, user, reason="", now=timezone.now())
        mock_refund_create.assert_not_called()

        reserved_ticket.refresh_from_db()
        assert reserved_ticket.status == Ticket.TicketStatus.CANCELLED
        tier.refresh_from_db()
        assert tier.quantity_sold == 1  # Released once, at cancel time.

        # Act: the orphaned PENDING payment is already expired.
        result = cleanup_expired_payments()

        # Assert
        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 1  # Must NOT drop to 0 -- that would be a double-decrement.
        assert not Payment.objects.filter(pk=payment.pk).exists()
        assert Ticket.objects.filter(pk=reserved_ticket.pk, status=Ticket.TicketStatus.CANCELLED).exists()
