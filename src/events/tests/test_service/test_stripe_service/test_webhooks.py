"""Tests for Stripe webhook event handling."""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketTier
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


class TestStripeEventHandler:
    """Test StripeEventHandler class."""

    @pytest.fixture
    def mock_stripe_event(self) -> MagicMock:
        """
        Creates a generic, robust mock of a Stripe webhook event
        that can be correctly converted to a dict.
        """
        event_data = {"id": "evt_generic", "type": "test.event", "data": {"object": {}}}
        # Use MagicMock for more flexibility and to mock magic methods
        mock_event = MagicMock(spec=stripe.Event)

        # This is the key fix: make the mock iterable like a dict
        mock_event.__iter__.return_value = iter(event_data.items())

        # Also configure attributes for other tests to pass
        mock_event.type = event_data["type"]
        mock_event.data = MagicMock()
        mock_event.data.object = event_data["data"]["object"]  # type: ignore[index]

        return mock_event

    @pytest.fixture
    def handler(self, mock_stripe_event: MagicMock) -> StripeEventHandler:
        """Create handler instance."""
        return StripeEventHandler(mock_stripe_event)

    @pytest.fixture
    def paid_ticket_tier(self, event: Event) -> TicketTier:
        """A paid ticket tier for testing."""
        gat = event.ticket_tiers.first()
        assert gat is not None
        gat.price = Decimal("25.00")
        gat.save()
        return gat

    @pytest.fixture
    def completed_payment(
        self,
        event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """Create a payment for testing webhooks."""
        ticket = Ticket.objects.create(
            guest_name="Test Guest",
            event=event,
            tier=paid_ticket_tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test123",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_routes_known_event_to_handler(self, handler: StripeEventHandler) -> None:
        """Test that known events are routed to appropriate handlers."""
        # Arrange
        handler.event.type = "checkout.session.completed"
        with patch.object(handler, "handle_checkout_session_completed") as mock_handler:
            # Act
            handler.handle()

            # Assert
            mock_handler.assert_called_once_with(handler.event)

    def test_routes_unknown_event_to_default_handler(
        self,
        handler: StripeEventHandler,
    ) -> None:
        """Test that unknown events are handled gracefully."""
        # Arrange
        handler.event.type = "unknown.event.type"

        with patch.object(handler, "handle_unknown_event") as mock_handler:
            # Act
            handler.handle()

            # Assert
            mock_handler.assert_called_once_with(handler.event)

    def test_handle_unknown_event_logs_only(
        self,
        handler: StripeEventHandler,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that unknown events are logged but don't raise exceptions."""
        # Arrange
        handler.event.type = "unknown.event.type"
        handler.event.id = "evt_test123"

        # Act
        handler.handle_unknown_event(handler.event)

        # Assert
        assert "stripe_webhook_unhandled_event" in caplog.text
        assert "unknown.event.type" in caplog.text

    @patch("notifications.signals.notification_requested.send")
    def test_handle_checkout_session_completed_success(
        self,
        mock_notification_signal: Mock,
        handler: StripeEventHandler,
        completed_payment: Payment,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test successful checkout session completion."""
        # Arrange
        mock_session_data = {
            "id": "cs_test123",
            "payment_status": "paid",
            "payment_intent": "pi_test123",
        }

        # Create a dictionary representing the full event for the test
        event_dict_data = {"type": "checkout.session.completed", "data": {"object": mock_session_data}}

        # Configure the mock event to be iterable and have the correct attributes
        handler.event.type = event_dict_data["type"]
        handler.event.data.object = event_dict_data["data"]["object"]  # type: ignore[index]
        handler.event.__iter__.return_value = iter(event_dict_data.items())  # type: ignore[attr-defined]

        # Act
        with django_capture_on_commit_callbacks(execute=True):
            handler.handle_checkout_session_completed(handler.event)

        # Assert
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.PaymentStatus.SUCCEEDED
        assert completed_payment.stripe_payment_intent_id == "pi_test123"
        # The assertion now works because handler.event is iterable
        assert completed_payment.raw_response == dict(handler.event)

        ticket = completed_payment.ticket
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.ACTIVE

        # Verify notification signals were sent
        # We expect multiple notifications: PAYMENT_CONFIRMATION, and potentially TICKET_UPDATED/TICKET_CREATED
        assert mock_notification_signal.called
        from notifications.enums import NotificationType

        # Check that PAYMENT_CONFIRMATION was sent
        payment_confirmation_calls = [
            call
            for call in mock_notification_signal.call_args_list
            if call.kwargs["notification_type"] == NotificationType.PAYMENT_CONFIRMATION
        ]
        assert len(payment_confirmation_calls) == 1
        assert payment_confirmation_calls[0].kwargs["user"] == completed_payment.user

    def test_handle_checkout_session_not_complete_is_noop(
        self,
        handler: StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that a webhook for a session that is not 'complete' is ignored."""
        # Arrange
        mock_session_data = {"id": "cs_test123", "payment_status": "unpaid"}
        handler.event.data.object = mock_session_data

        # Act
        handler.handle_checkout_session_completed(handler.event)

        # Assert
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.PaymentStatus.PENDING  # Status remains unchanged

    def test_handle_checkout_session_completed_idempotent(
        self,
        handler: StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that duplicate webhooks are handled idempotently.

        When a payment is already SUCCEEDED and we receive another webhook,
        the status doesn't change and no duplicate notification is sent
        (notifications are only sent when status changes via signal).
        """
        # Arrange
        completed_payment.status = Payment.PaymentStatus.SUCCEEDED
        completed_payment.save()

        mock_session_data = {"id": "cs_test123", "payment_status": "paid"}
        handler.event.data.object = mock_session_data

        # Act
        handler.handle_checkout_session_completed(handler.event)

        # Assert - payment status remains unchanged
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.PaymentStatus.SUCCEEDED
        assert "stripe_webhook_duplicate_payment_success" in caplog.text

    def test_handle_checkout_session_completed_payment_not_found(
        self,
        handler: StripeEventHandler,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that missing payment is logged and handled gracefully."""
        # Arrange
        mock_session_data = {"id": "cs_nonexistent", "payment_status": "paid"}
        handler.event.data.object = mock_session_data

        # Act
        handler.handle_checkout_session_completed(handler.event)

        # Assert - should log warning and return without error
        assert "stripe_session_no_payments" in caplog.text

    @patch("notifications.signals.notification_requested.send")
    def test_handle_charge_refunded_success(
        self,
        mock_notification_signal: Mock,
        handler: StripeEventHandler,
        completed_payment: Payment,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Test successful refund processing."""
        # Arrange
        completed_payment.status = Payment.PaymentStatus.SUCCEEDED
        completed_payment.stripe_payment_intent_id = "pi_test123"
        completed_payment.save()

        ticket = completed_payment.ticket
        ticket.status = Ticket.TicketStatus.ACTIVE
        ticket.save()

        tier = ticket.tier
        tier.quantity_sold = 5
        tier.save()

        mock_charge_data = {
            "id": "ch_test123",
            "payment_intent": "pi_test123",
        }

        event_dict_data = {"type": "charge.refunded", "data": {"object": mock_charge_data}}
        handler.event.type = event_dict_data["type"]
        handler.event.data.object = event_dict_data["data"]["object"]  # type: ignore[index]
        handler.event.__iter__.return_value = iter(event_dict_data.items())  # type: ignore[attr-defined]

        # Act - capture on_commit callbacks from signal handlers
        with django_capture_on_commit_callbacks(execute=True):
            handler.handle_charge_refunded(handler.event)

        # Assert
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.PaymentStatus.REFUNDED
        assert completed_payment.raw_response == dict(handler.event)

        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED

        tier.refresh_from_db()
        assert tier.quantity_sold == 4  # Restored from 5 to 4

        # Verify notification signal was sent to ticket holder (and potentially staff)
        # The signal is called at least once for the ticket holder
        # It may be called additional times for staff/owners with the preference enabled
        assert mock_notification_signal.call_count >= 1

        # Verify the first call is to the ticket holder
        first_call_kwargs = mock_notification_signal.call_args_list[0].kwargs
        assert first_call_kwargs["user"] == completed_payment.user
        from notifications.enums import NotificationType

        assert first_call_kwargs["notification_type"] == NotificationType.TICKET_REFUNDED
        # Context should include refund amount
        assert "refund_amount" in first_call_kwargs["context"]
        assert first_call_kwargs["context"]["ticket_id"] == str(ticket.id)

    def test_handle_charge_refunded_idempotent(
        self,
        handler: StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that duplicate refund webhooks are handled idempotently."""
        # Arrange
        completed_payment.status = Payment.PaymentStatus.REFUNDED
        completed_payment.stripe_payment_intent_id = "pi_test123"
        completed_payment.save()

        mock_charge_data = {
            "id": "ch_test123",
            "payment_intent": "pi_test123",
        }
        handler.event.data.object = mock_charge_data

        # Act
        handler.handle_charge_refunded(handler.event)

        # Assert
        assert "stripe_webhook_duplicate_refund" in caplog.text

    def test_handle_charge_refunded_unknown_payment(
        self,
        handler: StripeEventHandler,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test refund webhook for unknown payment is logged."""
        # Arrange
        mock_charge_data = {
            "id": "ch_test123",
            "payment_intent": "pi_unknown",
        }
        handler.event.data.object = mock_charge_data

        # Act
        handler.handle_charge_refunded(handler.event)

        # Assert
        assert "stripe_refund_unknown_intent" in caplog.text

    def test_handle_payment_intent_canceled_success(
        self,
        handler: StripeEventHandler,
        completed_payment: Payment,
    ) -> None:
        """Test successful payment intent cancellation processing."""
        # Arrange
        completed_payment.status = Payment.PaymentStatus.PENDING
        completed_payment.stripe_payment_intent_id = "pi_test123"
        completed_payment.save()

        ticket = completed_payment.ticket
        ticket.status = Ticket.TicketStatus.PENDING
        ticket.save()

        tier = ticket.tier
        tier.quantity_sold = 5
        tier.save()

        mock_payment_intent_data = {
            "id": "pi_test123",
            "status": "canceled",
        }

        event_dict_data = {"type": "payment_intent.canceled", "data": {"object": mock_payment_intent_data}}
        handler.event.type = event_dict_data["type"]
        handler.event.data.object = event_dict_data["data"]["object"]  # type: ignore[index]
        handler.event.__iter__.return_value = iter(event_dict_data.items())  # type: ignore[attr-defined]

        # Act
        handler.handle_payment_intent_canceled(handler.event)

        # Assert
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.PaymentStatus.FAILED
        assert completed_payment.raw_response == dict(handler.event)

        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED

        tier.refresh_from_db()
        assert tier.quantity_sold == 4  # Restored from 5 to 4

    def test_handle_payment_intent_canceled_non_pending_ignored(
        self,
        handler: StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that payment_intent.canceled for non-pending payment is ignored."""
        # Arrange
        completed_payment.status = Payment.PaymentStatus.SUCCEEDED
        completed_payment.stripe_payment_intent_id = "pi_test123"
        completed_payment.save()

        mock_payment_intent_data = {
            "id": "pi_test123",
            "status": "canceled",
        }
        handler.event.data.object = mock_payment_intent_data

        # Act
        handler.handle_payment_intent_canceled(handler.event)

        # Assert
        assert "stripe_payment_intent_canceled_no_pending" in caplog.text
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.PaymentStatus.SUCCEEDED  # Unchanged

    def test_handle_payment_intent_canceled_unknown_payment(
        self,
        handler: StripeEventHandler,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test payment_intent.canceled for unknown payment is logged as debug."""
        # Arrange
        mock_payment_intent_data = {
            "id": "pi_unknown",
            "status": "canceled",
        }
        handler.event.data.object = mock_payment_intent_data

        # Act
        handler.handle_payment_intent_canceled(handler.event)

        # Assert - No error raised, just logged at debug level
        # Note: caplog won't capture debug logs by default, but we're just checking it doesn't crash
