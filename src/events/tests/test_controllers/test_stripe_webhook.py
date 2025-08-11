"""Tests for the Stripe webhook controller."""

import json
from unittest.mock import Mock, patch

import pytest
import stripe
from django.conf import settings
from django.test.client import Client
from ninja.errors import HttpError

from events.controllers.stripe_webhook import StripeWebhookController

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

    @patch("stripe.Webhook.construct_event")
    @patch("events.service.stripe_service.StripeEventHandler")
    def test_handle_webhook_success(
        self,
        mock_handler_class: Mock,
        mock_construct_event: Mock,
        controller: StripeWebhookController,
        mock_request: Mock,
        mock_stripe_event: Mock,
    ) -> None:
        """Test successful webhook handling."""
        # Arrange
        mock_construct_event.return_value = mock_stripe_event
        mock_handler_instance = Mock()
        mock_handler_class.return_value = mock_handler_instance

        # Act
        status, response = controller.handle_webhook(mock_request)

        # Assert
        mock_construct_event.assert_called_once_with(
            mock_request.body,
            "t=123,v1=signature",
            settings.STRIPE_WEBHOOK_SECRET,  # from settings
        )
        mock_handler_class.assert_called_once_with(mock_stripe_event)
        mock_handler_instance.handle.assert_called_once()
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
        with pytest.raises(HttpError) as exc_info:
            controller.handle_webhook(mock_request)

        assert exc_info.value.status_code == 400
        assert "Invalid Stripe signature" in str(exc_info.value.message)

    @patch("stripe.Webhook.construct_event")
    def test_handle_webhook_invalid_signature(
        self,
        mock_construct_event: Mock,
        controller: StripeWebhookController,
        mock_request: Mock,
    ) -> None:
        """Test webhook handling with invalid signature."""
        # Arrange
        mock_construct_event.side_effect = stripe.error.SignatureVerificationError("Invalid signature", "sig_header")

        # Act & Assert
        with pytest.raises(stripe.error.SignatureVerificationError):
            controller.handle_webhook(mock_request)

        mock_construct_event.assert_called_once_with(
            mock_request.body, "t=123,v1=signature", settings.STRIPE_WEBHOOK_SECRET
        )

    @patch("stripe.Webhook.construct_event")
    @patch("events.service.stripe_service.StripeEventHandler")
    def test_handle_webhook_handler_exception(
        self,
        mock_handler_class: Mock,
        mock_construct_event: Mock,
        controller: StripeWebhookController,
        mock_request: Mock,
        mock_stripe_event: Mock,
    ) -> None:
        """Test webhook handling when handler raises exception."""
        # Arrange
        mock_construct_event.return_value = mock_stripe_event
        mock_handler_instance = Mock()
        mock_handler_instance.handle.side_effect = Exception("Handler error")
        mock_handler_class.return_value = mock_handler_instance

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

    @patch("stripe.Webhook.construct_event")
    @patch("events.service.stripe_service.StripeEventHandler")
    def test_webhook_endpoint_integration(
        self,
        mock_handler_class: Mock,
        mock_construct_event: Mock,
        client: Client,
        webhook_payload: bytes,
    ) -> None:
        """Test the webhook endpoint through Django's test client."""
        # Arrange
        mock_event = Mock(spec=stripe.Event)
        mock_event.type = "checkout.session.completed"
        mock_construct_event.return_value = mock_event

        mock_handler_instance = Mock()
        mock_handler_class.return_value = mock_handler_instance

        # Act
        response = client.post(
            "/api/stripe/webhook",
            data=webhook_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=123,v1=signature",
        )

        # Assert
        assert response.status_code == 200
        mock_construct_event.assert_called_once()
        mock_handler_class.assert_called_once_with(mock_event)
        mock_handler_instance.handle.assert_called_once()

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
        assert response.status_code == 400
        response_data = response.json()
        assert "Invalid Stripe signature" in response_data["detail"]
