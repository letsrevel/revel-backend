"""Tests for the Stripe webhook controller."""

import json
import time
from unittest.mock import Mock, patch

import pytest
import stripe
from django.test import override_settings
from django.test.client import Client

from events.controllers.stripe_webhook import StripeWebhookController
from events.exceptions import InvalidStripeWebhookSignatureError

pytestmark = pytest.mark.django_db


class TestStripeWebhookController:
    """Test StripeWebhookController."""

    @pytest.fixture
    def controller(self) -> StripeWebhookController:
        """Create controller instance."""
        return StripeWebhookController()

    @pytest.fixture
    def mock_request(self) -> Mock:
        """Mock HTTP request."""
        request = Mock()
        request.body = b'{"test": "data"}'
        request.META = {"HTTP_STRIPE_SIGNATURE": "t=123,v1=signature"}
        return request

    @pytest.fixture
    def mock_stripe_event(self) -> Mock:
        """Mock Stripe event."""
        event = Mock(spec=stripe.Event)
        event.type = "checkout.session.completed"
        return event

    @patch("events.controllers.stripe_webhook.stripe_webhooks.handle_event")
    @patch("events.controllers.stripe_webhook.stripe_webhooks.verify_webhook")
    def test_handle_webhook_success(
        self,
        mock_verify: Mock,
        mock_handle_event: Mock,
        controller: StripeWebhookController,
        mock_request: Mock,
        mock_stripe_event: Mock,
    ) -> None:
        """Test successful webhook handling."""
        # Arrange
        mock_verify.return_value = mock_stripe_event

        # Act
        status, response = controller.handle_webhook(mock_request)

        # Assert
        mock_verify.assert_called_once_with(mock_request.body, "t=123,v1=signature")
        mock_handle_event.assert_called_once_with(mock_stripe_event)
        assert status == 200
        assert response is None

    def test_handle_webhook_missing_signature(
        self,
        controller: StripeWebhookController,
        mock_request: Mock,
    ) -> None:
        """Test webhook handling with missing signature."""
        # Arrange
        mock_request.META = {}  # No signature header

        # Act & Assert
        with pytest.raises(InvalidStripeWebhookSignatureError):
            controller.handle_webhook(mock_request)

    @patch("events.controllers.stripe_webhook.stripe_webhooks.verify_webhook")
    def test_handle_webhook_invalid_signature(
        self,
        mock_verify: Mock,
        controller: StripeWebhookController,
        mock_request: Mock,
    ) -> None:
        """Test webhook handling with invalid signature."""
        # Arrange
        mock_verify.side_effect = InvalidStripeWebhookSignatureError()

        # Act & Assert
        with pytest.raises(InvalidStripeWebhookSignatureError):
            controller.handle_webhook(mock_request)

        mock_verify.assert_called_once_with(mock_request.body, "t=123,v1=signature")

    @patch("events.controllers.stripe_webhook.stripe_webhooks.handle_event")
    @patch("events.controllers.stripe_webhook.stripe_webhooks.verify_webhook")
    def test_handle_webhook_handler_exception(
        self,
        mock_verify: Mock,
        mock_handle_event: Mock,
        controller: StripeWebhookController,
        mock_request: Mock,
        mock_stripe_event: Mock,
    ) -> None:
        """Test webhook handling when handler raises exception."""
        # Arrange
        mock_verify.return_value = mock_stripe_event
        mock_handle_event.side_effect = Exception("Handler error")

        # Act & Assert
        with pytest.raises(Exception) as exc_info:
            controller.handle_webhook(mock_request)

        assert "Handler error" in str(exc_info.value)


class TestStripeWebhookIntegration:
    """Integration tests for the Stripe webhook endpoint."""

    @pytest.fixture
    def webhook_payload(self) -> bytes:
        """Sample webhook payload."""
        payload = {
            "id": "evt_test123",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test123", "object": "checkout_session", "status": "complete"}},
        }
        return json.dumps(payload).encode()

    @patch("events.controllers.stripe_webhook.stripe_webhooks.handle_event")
    @patch("events.controllers.stripe_webhook.stripe_webhooks.verify_webhook")
    def test_webhook_endpoint_integration(
        self,
        mock_verify: Mock,
        mock_handle_event: Mock,
        client: Client,
        webhook_payload: bytes,
    ) -> None:
        """Test the webhook endpoint through Django's test client."""
        # Arrange
        mock_event = Mock(spec=stripe.Event)
        mock_event.type = "checkout.session.completed"
        mock_verify.return_value = mock_event

        # Act
        response = client.post(
            "/api/stripe/webhook",
            data=webhook_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=123,v1=signature",
        )

        # Assert
        assert response.status_code == 200
        mock_verify.assert_called_once()
        mock_handle_event.assert_called_once_with(mock_event)

    def test_webhook_endpoint_missing_signature_integration(
        self,
        client: Client,
        webhook_payload: bytes,
    ) -> None:
        """Test webhook endpoint with missing signature through Django client."""
        # Act
        response = client.post(
            "/api/stripe/webhook",
            data=webhook_payload,
            content_type="application/json",
            # No Stripe signature header
        )

        # Assert
        assert response.status_code == 403
        response_data = response.json()
        assert "Invalid Stripe signature" in response_data["detail"]

    @override_settings(STRIPE_WEBHOOK_SECRETS=["whsec_configured"])
    def test_webhook_endpoint_wrong_secret_returns_403(
        self,
        client: Client,
        webhook_payload: bytes,
    ) -> None:
        """A delivery signed with a secret we don't hold answers 403, not 500."""
        timestamp = int(time.time())
        signature = stripe.WebhookSignature._compute_signature(  # noqa: SLF001
            f"{timestamp}.{webhook_payload.decode()}", "whsec_attacker"
        )

        response = client.post(
            "/api/stripe/webhook",
            data=webhook_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={signature}",
        )

        assert response.status_code == 403
        assert "Invalid Stripe signature" in response.json()["detail"]

class TestSubscriptionWebhookDispatch:
    """Phase 2 — the ``StripeEventHandler`` routes subscription/invoice events.

    Each handler delegates to the Stripe service; here we patch the service
    helpers and assert dispatch + payload propagation.
    """

    def _make_event(self, event_type: str, obj: dict[str, object]) -> Mock:
        event = Mock(spec=stripe.Event)
        event.type = event_type
        event.data = Mock()
        event.data.object = obj
        return event

    @patch("events.service.subscription_stripe_service.sync_subscription_from_stripe")
    def test_customer_subscription_created_dispatch(self, mock_sync: Mock) -> None:
        from events.service.stripe_webhooks import StripeEventHandler

        event = self._make_event(
            "customer.subscription.created",
            {"id": "sub_x", "status": "incomplete"},
        )
        StripeEventHandler(event).handle()
        mock_sync.assert_called_once()
        passed = mock_sync.call_args.args[0]
        assert passed["id"] == "sub_x"

    @patch("events.service.subscription_stripe_service.sync_subscription_from_stripe")
    def test_customer_subscription_updated_dispatch(self, mock_sync: Mock) -> None:
        from events.service.stripe_webhooks import StripeEventHandler

        event = self._make_event(
            "customer.subscription.updated",
            {"id": "sub_x", "status": "active", "cancel_at_period_end": True},
        )
        StripeEventHandler(event).handle()
        mock_sync.assert_called_once()

    @patch("events.service.subscription_stripe_service.sync_subscription_from_stripe")
    def test_customer_subscription_deleted_dispatch(self, mock_sync: Mock) -> None:
        from events.service.stripe_webhooks import StripeEventHandler

        event = self._make_event(
            "customer.subscription.deleted",
            {"id": "sub_x", "status": "canceled"},
        )
        StripeEventHandler(event).handle()
        mock_sync.assert_called_once()

    @patch("events.service.subscription_stripe_service.record_stripe_payment_from_invoice")
    def test_invoice_paid_dispatch(self, mock_record: Mock) -> None:
        from events.service.stripe_webhooks import StripeEventHandler

        event = self._make_event(
            "invoice.paid",
            {"id": "in_x", "subscription": "sub_x", "amount_paid": 1000, "currency": "eur"},
        )
        StripeEventHandler(event).handle()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["succeeded"] is True

    @patch("events.service.subscription_stripe_service.record_stripe_payment_from_invoice")
    def test_invoice_payment_failed_dispatch(self, mock_record: Mock) -> None:
        from events.service.stripe_webhooks import StripeEventHandler

        event = self._make_event(
            "invoice.payment_failed",
            {"id": "in_x", "subscription": "sub_x", "amount_due": 1000, "currency": "eur"},
        )
        StripeEventHandler(event).handle()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["succeeded"] is False
