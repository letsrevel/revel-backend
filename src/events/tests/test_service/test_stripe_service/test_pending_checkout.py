"""Tests for pending checkout resume and cancel functions."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket
from events.service import stripe_service

pytestmark = pytest.mark.django_db


class TestResumePendingCheckout:
    """Tests for resume_pending_checkout function."""

    @pytest.fixture
    def pending_payment(
        self,
        event: Event,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """Create a pending payment for testing."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.quantity_sold = 1
        tier.save()
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test Guest",
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test_resume",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_returns_404_when_payment_not_found(
        self,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 when payment doesn't exist."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.resume_pending_checkout("00000000-0000-0000-0000-000000000000", organization_owner_user)
        assert exc_info.value.status_code == 404
        assert "No pending payment found" in str(exc_info.value.message)

    def test_returns_404_when_payment_not_owned_by_user(
        self,
        pending_payment: Payment,
        member_user: RevelUser,
    ) -> None:
        """Should return 404 when payment belongs to different user."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.resume_pending_checkout(str(pending_payment.id), member_user)
        assert exc_info.value.status_code == 404

    def test_returns_404_when_payment_expired(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 and clean up when payment has expired."""
        # Make payment expired
        pending_payment.expires_at = timezone.now() - timedelta(hours=1)
        pending_payment.save()
        ticket_id = pending_payment.ticket_id

        with pytest.raises(HttpError) as exc_info:
            stripe_service.resume_pending_checkout(str(pending_payment.id), organization_owner_user)

        assert exc_info.value.status_code == 404
        assert "expired" in str(exc_info.value.message).lower()
        # Verify cleanup
        assert not Payment.objects.filter(id=pending_payment.id).exists()
        assert not Ticket.objects.filter(id=ticket_id).exists()

    @patch("stripe.checkout.Session.retrieve")
    def test_returns_checkout_url_for_valid_payment(
        self,
        mock_session_retrieve: Mock,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return Stripe checkout URL for valid pending payment."""
        mock_session_retrieve.return_value = Mock(url="https://checkout.stripe.com/test")

        result = stripe_service.resume_pending_checkout(str(pending_payment.id), organization_owner_user)

        assert result == "https://checkout.stripe.com/test"
        mock_session_retrieve.assert_called_once()


class TestCancelPendingCheckout:
    """Tests for cancel_pending_checkout function."""

    @pytest.fixture
    def pending_payment(
        self,
        event: Event,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """Create a pending payment for testing."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.quantity_sold = 1
        tier.save()
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test Guest",
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test_cancel",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_returns_404_when_payment_not_found(
        self,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 when payment doesn't exist."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.cancel_pending_checkout("00000000-0000-0000-0000-000000000000", organization_owner_user)
        assert exc_info.value.status_code == 404
        assert "Payment not found" in str(exc_info.value.message)

    def test_returns_404_when_payment_not_owned_by_user(
        self,
        pending_payment: Payment,
        member_user: RevelUser,
    ) -> None:
        """Should return 404 when payment belongs to different user."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.cancel_pending_checkout(str(pending_payment.id), member_user)
        assert exc_info.value.status_code == 404

    def test_returns_400_when_payment_not_pending(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 400 when payment is not in PENDING status."""
        pending_payment.status = Payment.PaymentStatus.SUCCEEDED
        pending_payment.save()

        with pytest.raises(HttpError) as exc_info:
            stripe_service.cancel_pending_checkout(str(pending_payment.id), organization_owner_user)
        assert exc_info.value.status_code == 400
        assert "Only pending payments" in str(exc_info.value.message)

    def test_deletes_payment_and_ticket(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should delete payment and ticket on successful cancel."""
        payment_id = pending_payment.id
        ticket_id = pending_payment.ticket_id

        result = stripe_service.cancel_pending_checkout(str(pending_payment.id), organization_owner_user)

        assert result == 1
        assert not Payment.objects.filter(id=payment_id).exists()
        assert not Ticket.objects.filter(id=ticket_id).exists()

    def test_decrements_quantity_sold(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should decrement tier's quantity_sold."""
        tier = pending_payment.ticket.tier
        initial_sold = tier.quantity_sold

        stripe_service.cancel_pending_checkout(str(pending_payment.id), organization_owner_user)

        tier.refresh_from_db()
        assert tier.quantity_sold == initial_sold - 1

    def test_deletes_all_tickets_in_batch(
        self,
        event: Event,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should delete all tickets with same stripe_session_id."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.quantity_sold = 3
        tier.save()

        # Create 3 tickets with same session_id (batch purchase)
        session_id = "cs_test_batch"
        tickets = []
        payments = []
        for i in range(3):
            ticket = Ticket.objects.create(
                event=event,
                tier=tier,
                user=organization_owner_user,
                status=Ticket.TicketStatus.PENDING,
                guest_name=f"Guest {i}",
            )
            tickets.append(ticket)
            payment = Payment.objects.create(
                ticket=ticket,
                user=organization_owner_user,
                stripe_session_id=session_id,
                amount=Decimal("25.00"),
                platform_fee=Decimal("1.25"),
                currency="EUR",
                status=Payment.PaymentStatus.PENDING,
                raw_response={},
            )
            payments.append(payment)

        # Cancel using any payment in the batch
        result = stripe_service.cancel_pending_checkout(str(payments[0].id), organization_owner_user)

        assert result == 3
        # All tickets and payments deleted
        for ticket in tickets:
            assert not Ticket.objects.filter(id=ticket.id).exists()
        for payment in payments:
            assert not Payment.objects.filter(id=payment.id).exists()
        # quantity_sold decremented by 3
        tier.refresh_from_db()
        assert tier.quantity_sold == 0
