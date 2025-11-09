"""Tests for Pay What You Can (PWYC) ticket functionality."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.schema import PWYCCheckoutPayloadSchema
from events.service.event_manager import EventManager
from events.service.ticket_service import TicketService

pytestmark = pytest.mark.django_db


# --- Model Tests ---


def test_pwyc_ticket_tier_creation(public_event: Event) -> None:
    """Test creation of PWYC ticket tier with valid fields."""
    tier = TicketTier.objects.create(
        event=public_event,
        name="PWYC Tier",
        price=Decimal("0"),  # Base price can be 0 for PWYC
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5"),
        pwyc_max=Decimal("50"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    assert tier.price_type == TicketTier.PriceType.PWYC
    assert tier.pwyc_min == Decimal("5")
    assert tier.pwyc_max == Decimal("50")


def test_pwyc_validation_max_less_than_min(public_event: Event) -> None:
    """Test validation fails when pwyc_max is less than pwyc_min."""
    tier = TicketTier(
        event=public_event,
        name="Invalid PWYC Tier",
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("50"),
        pwyc_max=Decimal("10"),  # Invalid: max < min
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    with pytest.raises(DjangoValidationError) as exc_info:
        tier.clean()

    assert "pwyc_max" in exc_info.value.message_dict
    assert "greater than or equal to minimum amount" in str(exc_info.value.message_dict["pwyc_max"])


def test_fixed_price_tier_defaults(public_event: Event) -> None:
    """Test that fixed price tiers have correct defaults."""
    tier = TicketTier.objects.create(
        event=public_event,
        name="Fixed Price Tier",
        price=Decimal("25"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    assert tier.price_type == TicketTier.PriceType.FIXED
    assert tier.pwyc_min == Decimal("1")  # Default value
    assert tier.pwyc_max is None


# --- Schema Tests ---


def test_pwyc_payload_schema_validation() -> None:
    """Test PWYC payload schema validation."""
    # Valid payload
    payload = PWYCCheckoutPayloadSchema(pwyc=Decimal("15"))
    assert payload.pwyc == Decimal("15")

    # Invalid payload - less than minimum
    with pytest.raises(ValueError):
        PWYCCheckoutPayloadSchema(pwyc=Decimal("0.50"))


# --- Controller Integration Tests ---


@patch("events.service.stripe_service.create_checkout_session")
def test_pwyc_checkout_with_valid_amount(mock_stripe: MagicMock, public_user: RevelUser, public_event: Event) -> None:
    """Test successful PWYC checkout with valid amount."""
    # Create PWYC tier
    tier = TicketTier.objects.create(
        event=public_event,
        name="PWYC Tier",
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("10"),
        pwyc_max=Decimal("100"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Mock Stripe response
    mock_stripe.return_value = ("https://checkout.stripe.com/pay/test", None)

    # Test checkout with valid PWYC amount
    manager = EventManager(public_user, public_event)
    result = manager.create_ticket(tier, price_override=Decimal("25"))

    # Verify Stripe was called with correct price override
    mock_stripe.assert_called_once_with(public_event, tier, public_user, price_override=Decimal("25"))
    assert isinstance(result, str)
    assert result.startswith("https://checkout.stripe.com")


@patch("events.service.stripe_service.create_checkout_session")
def test_pwyc_checkout_without_price_override_uses_base_price(
    mock_stripe: MagicMock, public_user: RevelUser, public_event: Event
) -> None:
    """Test that PWYC checkout without price_override uses base tier price."""
    # Create PWYC tier with base price
    tier = TicketTier.objects.create(
        event=public_event,
        name="PWYC Tier",
        price=Decimal("15"),  # Base price
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5"),
        pwyc_max=Decimal("50"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Mock Stripe response
    mock_stripe.return_value = ("https://checkout.stripe.com/pay/test", None)

    # Test checkout without price override
    manager = EventManager(public_user, public_event)
    manager.create_ticket(tier)

    # Verify Stripe was called without price override (None)
    mock_stripe.assert_called_once_with(public_event, tier, public_user, price_override=None)


def test_fixed_price_checkout_with_price_override_should_ignore(public_user: RevelUser, public_event: Event) -> None:
    """Test that fixed price tiers ignore price_override."""
    # Create fixed price tier
    tier = TicketTier.objects.create(
        event=public_event,
        name="Fixed Tier",
        price=Decimal("30"),
        price_type=TicketTier.PriceType.FIXED,
        payment_method=TicketTier.PaymentMethod.FREE,  # Use FREE to avoid Stripe mocking
    )

    # Test checkout with price override (should be ignored)
    manager = EventManager(public_user, public_event)
    ticket = manager.create_ticket(tier, price_override=Decimal("50"))

    # Should get a ticket (free tier) and ignore the price override
    from events.models import Ticket

    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


# --- Service Layer Tests ---


@patch("events.service.stripe_service.create_checkout_session")
def test_ticket_service_passes_price_override(
    mock_stripe: MagicMock, public_user: RevelUser, public_event: Event
) -> None:
    """Test that TicketService correctly passes price_override to Stripe."""
    tier = TicketTier.objects.create(
        event=public_event,
        name="PWYC Tier",
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Mock Stripe response
    mock_stripe.return_value = ("https://checkout.stripe.com/pay/test", None)

    service = TicketService(event=public_event, tier=tier, user=public_user)
    result = service.checkout(price_override=Decimal("20.0"))

    # Verify price override was passed to Stripe service
    mock_stripe.assert_called_once_with(public_event, tier, public_user, price_override=Decimal("20.0"))
    assert isinstance(result, str)


# --- Edge Cases ---


def test_pwyc_tier_with_no_max_limit(public_event: Event) -> None:
    """Test PWYC tier with no maximum limit."""
    tier = TicketTier.objects.create(
        event=public_event,
        name="Unlimited PWYC Tier",
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("1"),
        pwyc_max=None,  # No maximum
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Should not raise validation errors
    tier.clean()
    assert tier.pwyc_max is None


def test_stripe_service_uses_effective_price(public_user: RevelUser, public_event: Event) -> None:
    """Test that create_checkout_session uses effective_price correctly."""
    from events.service import stripe_service

    # Set up organization with Stripe
    public_event.organization.stripe_account_id = "acct_test"
    public_event.organization.stripe_details_submitted = True
    public_event.organization.stripe_charges_enabled = True
    public_event.organization.platform_fee_percent = Decimal("5")
    public_event.organization.platform_fee_fixed = Decimal("0.30")
    public_event.organization.save()

    tier = TicketTier.objects.create(
        event=public_event,
        name="PWYC Tier",
        price=Decimal("10"),  # Base price
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Mock the actual Stripe Session.create call
    with patch("events.service.stripe_service.Session.create") as mock_session_create:
        mock_session_create.return_value.id = "cs_test_session"
        mock_session_create.return_value.url = "https://checkout.stripe.com/pay/test"

        # Call with price override
        stripe_service.create_checkout_session(public_event, tier, public_user, price_override=Decimal("25.0"))

        # Verify the Stripe session was created with the overridden price
        mock_session_create.assert_called_once()
        call_args = mock_session_create.call_args[1]
        line_item = call_args["line_items"][0]

        # Should use overridden price (25.00) instead of tier price (10.00)
        assert line_item["price_data"]["unit_amount"] == 2500  # 25.00 * 100 cents
