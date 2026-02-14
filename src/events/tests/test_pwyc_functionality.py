"""Tests for Pay What You Can (PWYC) ticket functionality."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.schema import PWYCCheckoutPayloadSchema

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
