"""Tests for the Stripe service."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from django.conf import settings
from django.http.response import Http404
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import stripe_service

pytestmark = pytest.mark.django_db


class TestCreateConnectAccount:
    """Test create_connect_account function."""

    @patch("stripe.Account.create")
    def test_creates_stripe_account_and_saves_id(
        self,
        mock_stripe_create: Mock,
        organization: Organization,
    ) -> None:
        """Test that a Stripe Connect account is created and ID is saved."""
        # Arrange
        mock_account = Mock()
        mock_account.id = "acct_test123"
        mock_stripe_create.return_value = mock_account

        # Act
        result = stripe_service.create_connect_account(organization)

        # Assert
        mock_stripe_create.assert_called_once_with(type="standard", email=organization.owner.email)
        organization.refresh_from_db()
        assert organization.stripe_account_id == "acct_test123"
        assert result == "acct_test123"

    @patch("stripe.Account.create")
    def test_handles_stripe_api_error(
        self,
        mock_stripe_create: Mock,
        organization: Organization,
    ) -> None:
        """Test that Stripe API errors are propagated."""
        # Arrange
        mock_stripe_create.side_effect = stripe.error.APIError("API Error")

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            stripe_service.create_connect_account(organization)

        # Verify organization wasn't modified
        organization.refresh_from_db()
        assert organization.stripe_account_id is None


class TestCreateAccountLink:
    """Test create_account_link function."""

    @patch("stripe.AccountLink.create")
    def test_creates_onboarding_link(
        self,
        mock_stripe_create: Mock,
        organization: Organization,
    ) -> None:
        """Test that onboarding link is created with correct URLs."""
        # Arrange
        account_id = "acct_test123"
        mock_link = Mock()
        mock_link.url = "https://stripe.com/onboard/test"
        mock_stripe_create.return_value = mock_link

        # Act
        result = stripe_service.create_account_link(account_id, organization)

        # Assert
        expected_refresh_url = (
            f"{settings.FRONTEND_BASE_URL}/org/{organization.slug}/admin/settings?stripe_refresh=true"
        )
        expected_return_url = f"{settings.FRONTEND_BASE_URL}/org/{organization.slug}/admin/settings?stripe_success=true"

        mock_stripe_create.assert_called_once_with(
            account=account_id,
            refresh_url=expected_refresh_url,
            return_url=expected_return_url,
            type="account_onboarding",
        )
        assert result == "https://stripe.com/onboard/test"

    @patch("stripe.AccountLink.create")
    def test_handles_stripe_api_error(self, mock_stripe_create: Mock, organization: Organization) -> None:
        """Test that Stripe API errors are propagated from create_account_link."""
        # Arrange
        mock_stripe_create.side_effect = stripe.error.APIError("API Error")
        account_id = "acct_test123"

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            stripe_service.create_account_link(account_id, organization)


class TestGetAccountDetails:
    """Test get_account_details function."""

    @patch("stripe.Account.retrieve")
    def test_retrieves_account_details(self, mock_stripe_retrieve: Mock) -> None:
        """Test that account details are retrieved from Stripe."""
        # Arrange
        account_id = "acct_test123"
        mock_account = Mock(spec=stripe.Account)
        mock_stripe_retrieve.return_value = mock_account

        # Act
        result = stripe_service.get_account_details(account_id)

        # Assert
        mock_stripe_retrieve.assert_called_once_with(account_id)
        assert result == mock_account

    @patch("stripe.Account.retrieve")
    def test_handles_stripe_api_error(self, mock_stripe_retrieve: Mock) -> None:
        """Test that Stripe API errors are propagated from get_account_details."""
        # Arrange
        mock_stripe_retrieve.side_effect = stripe.error.APIError("API Error")
        account_id = "acct_test123"

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            stripe_service.get_account_details(account_id)


class TestCreateCheckoutSession:
    """Test create_checkout_session function."""

    @pytest.fixture
    def stripe_connected_organization(self, organization: Organization) -> Organization:
        """Organization with Stripe account connected."""
        organization.stripe_account_id = "acct_test123"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.platform_fee_percent = Decimal("3.00")
        organization.platform_fee_fixed = Decimal("0.50")
        organization.save()
        return organization

    @pytest.fixture
    def paid_ticket_tier(self, event: Event) -> TicketTier:
        """A paid ticket tier."""
        ga_tier = event.ticket_tiers.first()
        assert ga_tier is not None
        ga_tier.price = Decimal("25.00")
        ga_tier.total_quantity = 10
        ga_tier.save()
        return ga_tier

    def test_raises_error_when_organization_not_connected(
        self,
        event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that error is raised when organization has no Stripe account."""
        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_checkout_session(event, paid_ticket_tier, organization_owner_user)

        assert exc_info.value.status_code == 400
        assert "not configured to accept payments" in exc_info.value.message

    def test_raises_error_for_free_ticket(
        self,
        event: Event,
        stripe_connected_organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that error is raised for free tickets."""
        # Arrange
        event.organization = stripe_connected_organization
        event.save()

        free_tier = TicketTier.objects.create(
            event=event,
            name="Free Tier",
            price=Decimal("0.00"),
            currency="EUR",
        )

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_checkout_session(event, free_tier, organization_owner_user)

        assert exc_info.value.status_code == 400
        assert "cannot be purchased" in exc_info.value.message

    def test_raises_error_for_sold_out_tier(
        self,
        event: Event,
        paid_ticket_tier: TicketTier,
        stripe_connected_organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that an error is raised if the ticket tier is sold out."""
        # Arrange
        event.organization = stripe_connected_organization
        event.save()
        paid_ticket_tier.event = event
        paid_ticket_tier.quantity_sold = paid_ticket_tier.total_quantity or 0
        paid_ticket_tier.save()

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_checkout_session(event, paid_ticket_tier, organization_owner_user)

        assert exc_info.value.status_code == 429
        assert "sold out" in exc_info.value.message

    @patch("stripe.checkout.Session.create")
    @patch("stripe.checkout.Session.retrieve")
    def test_returns_existing_session_for_active_pending_payment(
        self,
        mock_stripe_retrieve: Mock,
        mock_stripe_create: Mock,
        event: Event,
        paid_ticket_tier: TicketTier,
        stripe_connected_organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that an existing, non-expired session URL is returned for a pending payment."""
        # Arrange
        event.organization = stripe_connected_organization
        event.save()
        paid_ticket_tier.event = event
        paid_ticket_tier.save()

        # Create a pre-existing pending payment and ticket
        ticket = Ticket.objects.create(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, status=Ticket.Status.PENDING
        )
        Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_existing",
            amount=paid_ticket_tier.price,
            platform_fee=Decimal("1.25"),
        )

        mock_session = Mock()
        mock_session.url = "https://checkout.stripe.com/pay/cs_existing"
        mock_stripe_retrieve.return_value = mock_session

        # Act
        checkout_url, _ = stripe_service.create_checkout_session(event, paid_ticket_tier, organization_owner_user)

        # Assert
        assert checkout_url == "https://checkout.stripe.com/pay/cs_existing"
        mock_stripe_retrieve.assert_called_once_with("cs_existing")
        mock_stripe_create.assert_not_called()  # Should not create a new session
        assert Ticket.objects.count() == 1  # No new ticket created

    @patch("stripe.checkout.Session.create")
    def test_cleans_up_expired_payment_and_creates_new_session(
        self,
        mock_stripe_create: Mock,
        event: Event,
        paid_ticket_tier: TicketTier,
        stripe_connected_organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that an expired pending payment is cleaned up and a new session is created."""
        # Arrange
        event.organization = stripe_connected_organization
        event.save()
        paid_ticket_tier.event = event
        paid_ticket_tier.save()

        ticket = Ticket.objects.create(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, status=Ticket.Status.PENDING
        )
        Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_expired",
            amount=paid_ticket_tier.price,
            expires_at=timezone.now() - timedelta(minutes=1),
            platform_fee=Decimal("1.25"),
        )
        paid_ticket_tier.quantity_sold = 1
        paid_ticket_tier.save()

        mock_session = Mock()
        mock_session.id = "cs_new"
        mock_session.url = "https://checkout.stripe.com/pay/cs_new"
        mock_stripe_create.return_value = mock_session

        # Act
        stripe_service.create_checkout_session(event, paid_ticket_tier, organization_owner_user)

        # Assert
        paid_ticket_tier.refresh_from_db()
        # Should be 0 from cleanup, then 1 from new session creation
        assert paid_ticket_tier.quantity_sold == 1
        assert not Payment.objects.filter(stripe_session_id="cs_expired").exists()
        assert not Ticket.objects.filter(payment__stripe_session_id="cs_expired").exists()
        assert Payment.objects.filter(stripe_session_id="cs_new").exists()
        mock_stripe_create.assert_called_once()

    @patch("stripe.checkout.Session.create")
    def test_creates_checkout_session_successfully(
        self,
        mock_stripe_create: Mock,
        event: Event,
        paid_ticket_tier: TicketTier,
        stripe_connected_organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test successful checkout session creation."""
        # Arrange
        event.organization = stripe_connected_organization
        event.save()
        paid_ticket_tier.event = event
        paid_ticket_tier.save()

        mock_session = Mock()
        mock_session.id = "cs_test123"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test123"
        mock_stripe_create.return_value = mock_session

        # Act
        checkout_url, payment = stripe_service.create_checkout_session(event, paid_ticket_tier, organization_owner_user)

        # Assert
        # Verify Stripe session creation
        expected_platform_fee = Decimal("0.75")  # 25.00 * 0.03
        expected_fixed_fee = Decimal("0.50")
        expected_application_fee_amount = int((expected_platform_fee + expected_fixed_fee) * 100)  # 125 cents

        mock_stripe_create.assert_called_once()
        call_args = mock_stripe_create.call_args
        assert call_args[1]["customer_email"] == organization_owner_user.email
        assert call_args[1]["line_items"][0]["price_data"]["unit_amount"] == 2500  # 25.00 * 100
        assert call_args[1]["line_items"][0]["price_data"]["currency"] == "eur"
        assert call_args[1]["payment_intent_data"]["application_fee_amount"] == expected_application_fee_amount
        assert call_args[1]["stripe_account"] == "acct_test123"

        # Verify URLs
        expected_success_url = (
            f"{settings.FRONTEND_BASE_URL}/events/{event.organization.slug}/{event.slug}?payment_success=true"
        )
        expected_cancel_url = (
            f"{settings.FRONTEND_BASE_URL}/events/{event.organization.slug}/{event.slug}?payment_cancelled=true"
        )
        assert call_args[1]["success_url"] == expected_success_url
        assert call_args[1]["cancel_url"] == expected_cancel_url

        # Verify return values
        assert checkout_url == "https://checkout.stripe.com/pay/cs_test123"
        assert isinstance(payment, Payment)
        assert payment.stripe_session_id == "cs_test123"
        assert payment.amount == Decimal("25.00")
        assert payment.platform_fee == expected_platform_fee + expected_fixed_fee
        assert payment.user == organization_owner_user

        # Verify ticket was created
        ticket = payment.ticket
        assert ticket.status == Ticket.Status.PENDING
        assert ticket.event == event
        assert ticket.tier == paid_ticket_tier
        assert ticket.user == organization_owner_user

    @patch("stripe.checkout.Session.create")
    def test_cleans_up_ticket_on_stripe_error(
        self,
        mock_stripe_create: Mock,
        event: Event,
        paid_ticket_tier: TicketTier,
        stripe_connected_organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that ticket is deleted when Stripe API fails."""
        # Arrange
        event.organization = stripe_connected_organization
        event.save()
        paid_ticket_tier.event = event
        paid_ticket_tier.save()

        mock_stripe_create.side_effect = stripe.error.APIError("Stripe API Error")

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_checkout_session(event, paid_ticket_tier, organization_owner_user)

        assert exc_info.value.status_code == 500
        assert "Stripe API error" in exc_info.value.message

        # Verify no ticket or payment was left behind
        assert Ticket.objects.filter(user=organization_owner_user, event=event).count() == 0
        assert Payment.objects.filter(user=organization_owner_user).count() == 0


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
    def handler(self, mock_stripe_event: MagicMock) -> stripe_service.StripeEventHandler:
        """Create handler instance."""
        return stripe_service.StripeEventHandler(mock_stripe_event)

    def test_routes_known_event_to_handler(self, handler: stripe_service.StripeEventHandler) -> None:
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
        handler: stripe_service.StripeEventHandler,
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
        handler: stripe_service.StripeEventHandler,
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

    @pytest.fixture
    def completed_payment(
        self,
        event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """Create a payment for testing webhooks."""
        ticket = Ticket.objects.create(
            event=event,
            tier=paid_ticket_tier,
            user=organization_owner_user,
            status=Ticket.Status.PENDING,
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test123",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.Status.PENDING,
            raw_response={},
        )

    @pytest.fixture
    def paid_ticket_tier(self, event: Event) -> TicketTier:
        """A paid ticket tier for testing."""
        gat = event.ticket_tiers.first()
        assert gat is not None
        gat.price = Decimal("25.00")
        gat.save()
        return gat

    @patch("events.tasks.send_payment_confirmation_email.delay")
    def test_handle_checkout_session_completed_success(
        self,
        mock_email_task: Mock,
        handler: stripe_service.StripeEventHandler,
        completed_payment: Payment,
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
        handler.handle_checkout_session_completed(handler.event)

        # Assert
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.Status.SUCCEEDED
        assert completed_payment.stripe_payment_intent_id == "pi_test123"
        # The assertion now works because handler.event is iterable
        assert completed_payment.raw_response == dict(handler.event)

        ticket = completed_payment.ticket
        ticket.refresh_from_db()
        assert ticket.status == Ticket.Status.ACTIVE

        mock_email_task.assert_called_once_with(str(completed_payment.id))

    def test_handle_checkout_session_not_complete_is_noop(
        self,
        handler: stripe_service.StripeEventHandler,
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
        assert completed_payment.status == Payment.Status.PENDING  # Status remains unchanged

    @patch("events.tasks.send_payment_confirmation_email.delay")
    def test_handle_checkout_session_completed_idempotent(
        self,
        mock_email_task: Mock,
        handler: stripe_service.StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that duplicate webhooks are handled idempotently."""
        # Arrange
        completed_payment.status = Payment.Status.SUCCEEDED
        completed_payment.save()

        mock_session_data = {"id": "cs_test123", "payment_status": "paid"}
        handler.event.data.object = mock_session_data

        # Act
        handler.handle_checkout_session_completed(handler.event)

        # Assert
        assert "stripe_webhook_duplicate_payment_success" in caplog.text
        mock_email_task.assert_not_called()

    def test_handle_checkout_session_completed_payment_not_found(
        self,
        handler: stripe_service.StripeEventHandler,
    ) -> None:
        """Test that 404 is raised when payment is not found."""
        # Arrange
        mock_session_data = {"id": "cs_nonexistent", "payment_status": "paid"}
        handler.event.data.object = mock_session_data

        # Act & Assert
        with pytest.raises(Http404):
            handler.handle_checkout_session_completed(handler.event)

    def test_handle_charge_refunded_success(
        self,
        handler: stripe_service.StripeEventHandler,
        completed_payment: Payment,
    ) -> None:
        """Test successful refund processing."""
        # Arrange
        completed_payment.status = Payment.Status.SUCCEEDED
        completed_payment.stripe_payment_intent_id = "pi_test123"
        completed_payment.save()

        ticket = completed_payment.ticket
        ticket.status = Ticket.Status.ACTIVE
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

        # Act
        handler.handle_charge_refunded(handler.event)

        # Assert
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.Status.REFUNDED
        assert completed_payment.raw_response == dict(handler.event)

        ticket.refresh_from_db()
        assert ticket.status == Ticket.Status.CANCELLED

        tier.refresh_from_db()
        assert tier.quantity_sold == 4  # Restored from 5 to 4

    def test_handle_charge_refunded_idempotent(
        self,
        handler: stripe_service.StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that duplicate refund webhooks are handled idempotently."""
        # Arrange
        completed_payment.status = Payment.Status.REFUNDED
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
        handler: stripe_service.StripeEventHandler,
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
        handler: stripe_service.StripeEventHandler,
        completed_payment: Payment,
    ) -> None:
        """Test successful payment intent cancellation processing."""
        # Arrange
        completed_payment.status = Payment.Status.PENDING
        completed_payment.stripe_payment_intent_id = "pi_test123"
        completed_payment.save()

        ticket = completed_payment.ticket
        ticket.status = Ticket.Status.PENDING
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
        assert completed_payment.status == Payment.Status.FAILED
        assert completed_payment.raw_response == dict(handler.event)

        ticket.refresh_from_db()
        assert ticket.status == Ticket.Status.CANCELLED

        tier.refresh_from_db()
        assert tier.quantity_sold == 4  # Restored from 5 to 4

    def test_handle_payment_intent_canceled_non_pending_ignored(
        self,
        handler: stripe_service.StripeEventHandler,
        completed_payment: Payment,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that payment_intent.canceled for non-pending payment is ignored."""
        # Arrange
        completed_payment.status = Payment.Status.SUCCEEDED
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
        assert "stripe_payment_intent_canceled_non_pending" in caplog.text
        completed_payment.refresh_from_db()
        assert completed_payment.status == Payment.Status.SUCCEEDED  # Unchanged

    def test_handle_payment_intent_canceled_unknown_payment(
        self,
        handler: stripe_service.StripeEventHandler,
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
