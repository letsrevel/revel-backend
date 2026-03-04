"""Tests for discount code service: validation and preview logic.

Tests cover:
- Full validation chain for authenticated users (validate_discount_code)
- Validation for anonymous (guest) users (validate_discount_code_anonymous)
- Preview discount code for both user types (preview_discount_code)

CRUD and price calculation tests are in test_discount_code_service.py.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    Ticket,
    TicketTier,
)
from events.models.discount_code import DiscountCode
from events.service import discount_code_service

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dc_owner(django_user_model: type[RevelUser]) -> RevelUser:
    """User who owns the organization for discount code tests."""
    return django_user_model.objects.create_user(
        username="dc_owner",
        email="dc_owner@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def dc_buyer(django_user_model: type[RevelUser]) -> RevelUser:
    """User who buys tickets and uses discount codes."""
    return django_user_model.objects.create_user(
        username="dc_buyer",
        email="dc_buyer@example.com",
        password="pass",
    )


@pytest.fixture
def dc_org(dc_owner: RevelUser) -> Organization:
    """Organization for discount code tests."""
    return Organization.objects.create(
        name="DC Test Org",
        slug="dc-test-org",
        owner=dc_owner,
    )


@pytest.fixture
def dc_series(dc_org: Organization) -> EventSeries:
    """Event series scoped to the test organization."""
    return EventSeries.objects.create(
        organization=dc_org,
        name="DC Series",
        slug="dc-series",
    )


@pytest.fixture
def dc_event(dc_org: Organization, dc_series: EventSeries) -> Event:
    """Event for discount code tests."""
    return Event.objects.create(
        organization=dc_org,
        name="DC Event",
        slug="dc-event",
        event_series=dc_series,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        start=timezone.now(),
        status="open",
        requires_ticket=True,
    )


@pytest.fixture
def dc_paid_tier(dc_event: Event) -> TicketTier:
    """A paid ticket tier (online, fixed price, EUR)."""
    return TicketTier.objects.create(
        event=dc_event,
        name="Paid Tier",
        price=Decimal("50.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.FIXED,
    )


@pytest.fixture
def dc_free_tier(dc_event: Event) -> TicketTier:
    """A free ticket tier."""
    return TicketTier.objects.create(
        event=dc_event,
        name="Free Tier",
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def dc_pwyc_tier(dc_event: Event) -> TicketTier:
    """A pay-what-you-can ticket tier."""
    return TicketTier.objects.create(
        event=dc_event,
        name="PWYC Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("1.00"),
    )


@pytest.fixture
def dc_percentage(dc_org: Organization) -> DiscountCode:
    """A 20% percentage discount code, active and org-wide."""
    return DiscountCode.objects.create(
        code="SAVE20",
        organization=dc_org,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("20.00"),
        is_active=True,
        max_uses_per_user=1,
    )


@pytest.fixture
def dc_fixed(dc_org: Organization) -> DiscountCode:
    """A fixed amount (EUR 10) discount code, active and org-wide."""
    return DiscountCode.objects.create(
        code="FLAT10",
        organization=dc_org,
        discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
        discount_value=Decimal("10.00"),
        currency="EUR",
        is_active=True,
        max_uses_per_user=1,
    )


# ===========================================================================
# validate_discount_code (authenticated users)
# ===========================================================================


class TestValidateDiscountCode:
    """Tests for the validate_discount_code function (authenticated users)."""

    def test_valid_percentage_code(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should return the DiscountCode when all checks pass."""
        result = discount_code_service.validate_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_percentage.pk

    def test_valid_fixed_amount_code(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_fixed: DiscountCode,
    ) -> None:
        """Should validate a fixed amount code when currency matches."""
        result = discount_code_service.validate_discount_code(
            code="FLAT10",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_fixed.pk

    def test_code_case_insensitive(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should find code regardless of input case (uppercased internally)."""
        result = discount_code_service.validate_discount_code(
            code="save20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_percentage.pk

    def test_code_not_found_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
    ) -> None:
        """Should raise HttpError 400 when the discount code does not exist."""
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="NONEXISTENT",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400

    def test_inactive_code_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise HttpError 400 when the code exists but is inactive."""
        dc_percentage.is_active = False
        dc_percentage.save(update_fields=["is_active"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400

    def test_not_yet_active_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when valid_from is in the future."""
        from datetime import timedelta

        dc_percentage.valid_from = timezone.now() + timedelta(days=7)
        dc_percentage.save(update_fields=["valid_from"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "not yet active" in str(exc_info.value.message)

    def test_expired_code_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when valid_until is in the past."""
        from datetime import timedelta

        dc_percentage.valid_until = timezone.now() - timedelta(days=1)
        dc_percentage.save(update_fields=["valid_until"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "expired" in str(exc_info.value.message)

    def test_max_uses_reached_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when the code has been used up to its max_uses."""
        dc_percentage.max_uses = 5
        dc_percentage.times_used = 5
        dc_percentage.save(update_fields=["max_uses", "times_used"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "usage limit" in str(exc_info.value.message)

    def test_per_user_max_uses_reached_raises_400(
        self,
        dc_org: Organization,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when user has already used the code max_uses_per_user times."""
        # Create an existing ticket that used this code
        Ticket.objects.create(
            event=dc_event,
            user=dc_buyer,
            tier=dc_paid_tier,
            guest_name="Buyer",
            discount_code=dc_percentage,
            status=Ticket.TicketStatus.ACTIVE,
        )

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "maximum number of times" in str(exc_info.value.message)

    def test_per_user_limit_considers_batch_size(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when batch_size exceeds remaining per-user allowance."""
        # max_uses_per_user=1 by default, batch_size=2 should fail
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=2,
            )

        assert exc_info.value.status_code == 400
        assert "maximum number of times" in str(exc_info.value.message)

    def test_free_tier_raises_400(
        self,
        dc_org: Organization,
        dc_free_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when applying a discount code to a free tier."""
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_free_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "free tickets" in str(exc_info.value.message)

    def test_pwyc_tier_raises_400(
        self,
        dc_org: Organization,
        dc_pwyc_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when applying a discount code to a PWYC tier."""
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_pwyc_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "pay-what-you-can" in str(exc_info.value.message)

    def test_scope_org_wide_no_m2m(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should pass validation when code has no M2M scope (org-wide)."""
        result = discount_code_service.validate_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_percentage.pk

    def test_scope_tier_match(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should pass when the code is scoped to the specific tier."""
        dc_percentage.tiers.add(dc_paid_tier)

        result = discount_code_service.validate_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_percentage.pk

    def test_scope_event_match(
        self,
        dc_org: Organization,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should pass when the code is scoped to the tier's event."""
        dc_percentage.events.add(dc_event)

        result = discount_code_service.validate_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_percentage.pk

    def test_scope_series_match(
        self,
        dc_org: Organization,
        dc_series: EventSeries,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should pass when the code is scoped to the tier's event series."""
        dc_percentage.series.add(dc_series)

        result = discount_code_service.validate_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=1,
        )

        assert result.pk == dc_percentage.pk

    def test_scope_no_match_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when code is scoped but tier/event/series do not match."""
        other_event = Event.objects.create(
            organization=dc_org,
            name="Other Event",
            slug="other-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=100,
            start=timezone.now(),
            status="open",
        )
        dc_percentage.events.add(other_event)

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "not valid for this ticket tier" in str(exc_info.value.message)

    def test_currency_mismatch_fixed_amount_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
    ) -> None:
        """Should raise 400 when a FIXED_AMOUNT code has a different currency than the tier."""
        DiscountCode.objects.create(
            code="USD10",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("10.00"),
            currency="USD",
            is_active=True,
        )

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="USD10",
                organization=dc_org,
                tier=dc_paid_tier,  # currency=EUR
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "not valid for this currency" in str(exc_info.value.message)

    def test_min_purchase_amount_not_met_raises_400(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
    ) -> None:
        """Should raise 400 when total purchase is below min_purchase_amount."""
        DiscountCode.objects.create(
            code="BIGORDER",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            min_purchase_amount=Decimal("200.00"),
            is_active=True,
        )

        # Tier price is 50 EUR, batch_size=1, so total = 50 < 200
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code(
                code="BIGORDER",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
                batch_size=1,
            )

        assert exc_info.value.status_code == 400
        assert "Minimum purchase amount" in str(exc_info.value.message)

    def test_min_purchase_amount_met_with_larger_batch(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
    ) -> None:
        """Should pass when batch_size * price meets min_purchase_amount."""
        dc_min = DiscountCode.objects.create(
            code="BIGBATCH",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            min_purchase_amount=Decimal("100.00"),
            max_uses_per_user=5,
            is_active=True,
        )

        # Tier price is 50 EUR, batch_size=2, so total = 100 >= 100
        result = discount_code_service.validate_discount_code(
            code="BIGBATCH",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
            batch_size=2,
        )

        assert result.pk == dc_min.pk


# ===========================================================================
# validate_discount_code_anonymous
# ===========================================================================


class TestValidateDiscountCodeAnonymous:
    """Tests for the validate_discount_code_anonymous function (guest users)."""

    def test_anonymous_valid_code(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should validate successfully for anonymous users with a valid code."""
        result = discount_code_service.validate_discount_code_anonymous(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
        )

        assert result.pk == dc_percentage.pk

    def test_anonymous_skips_per_user_check(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should not check per-user limits for anonymous users."""
        result = discount_code_service.validate_discount_code_anonymous(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
        )

        assert result.pk == dc_percentage.pk

    def test_anonymous_still_checks_global_limit(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when global max_uses is exhausted even for anonymous."""
        dc_percentage.max_uses = 10
        dc_percentage.times_used = 10
        dc_percentage.save(update_fields=["max_uses", "times_used"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
            )

        assert exc_info.value.status_code == 400
        assert "usage limit" in str(exc_info.value.message)

    def test_anonymous_expired_code(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 for expired codes even with anonymous users."""
        from datetime import timedelta

        dc_percentage.valid_until = timezone.now() - timedelta(days=1)
        dc_percentage.save(update_fields=["valid_until"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
            )

        assert exc_info.value.status_code == 400
        assert "expired" in str(exc_info.value.message)

    def test_anonymous_not_yet_active(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 when code is not yet active for anonymous users."""
        from datetime import timedelta

        dc_percentage.valid_from = timezone.now() + timedelta(days=7)
        dc_percentage.save(update_fields=["valid_from"])

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
            )

        assert exc_info.value.status_code == 400
        assert "not yet active" in str(exc_info.value.message)

    def test_anonymous_free_tier_raises_400(
        self,
        dc_org: Organization,
        dc_free_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 for free tiers even for anonymous users."""
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="SAVE20",
                organization=dc_org,
                tier=dc_free_tier,
            )

        assert exc_info.value.status_code == 400
        assert "free tickets" in str(exc_info.value.message)

    def test_anonymous_pwyc_tier_raises_400(
        self,
        dc_org: Organization,
        dc_pwyc_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 for PWYC tiers even for anonymous users."""
        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="SAVE20",
                organization=dc_org,
                tier=dc_pwyc_tier,
            )

        assert exc_info.value.status_code == 400
        assert "pay-what-you-can" in str(exc_info.value.message)

    def test_anonymous_currency_mismatch(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
    ) -> None:
        """Should raise 400 for currency mismatch with anonymous users."""
        DiscountCode.objects.create(
            code="USD5",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("5.00"),
            currency="USD",
            is_active=True,
        )

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="USD5",
                organization=dc_org,
                tier=dc_paid_tier,
            )

        assert exc_info.value.status_code == 400
        assert "not valid for this currency" in str(exc_info.value.message)

    def test_anonymous_scope_no_match(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should raise 400 for scope mismatch with anonymous users."""
        other_event = Event.objects.create(
            organization=dc_org,
            name="Scoped Event",
            slug="scoped-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            max_attendees=50,
            start=timezone.now(),
            status="open",
        )
        dc_percentage.events.add(other_event)

        with pytest.raises(HttpError) as exc_info:
            discount_code_service.validate_discount_code_anonymous(
                code="SAVE20",
                organization=dc_org,
                tier=dc_paid_tier,
            )

        assert exc_info.value.status_code == 400
        assert "not valid for this ticket tier" in str(exc_info.value.message)


# ===========================================================================
# preview_discount_code
# ===========================================================================


class TestPreviewDiscountCode:
    """Tests for the preview_discount_code function."""

    def test_preview_authenticated_user(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should return a DiscountCodeValidationResponse for authenticated users."""
        from events.schema.discount_code import DiscountCodeValidationResponse

        response = discount_code_service.preview_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
        )

        assert isinstance(response, DiscountCodeValidationResponse)
        assert response.valid is True
        assert response.discount_type == DiscountCode.DiscountType.PERCENTAGE
        assert response.discount_value == Decimal("20.00")
        # 50 * (100 - 20) / 100 = 40.00
        assert response.discounted_price == Decimal("40.00")

    def test_preview_anonymous_user(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_percentage: DiscountCode,
    ) -> None:
        """Should return a valid response for anonymous users."""
        from events.schema.discount_code import DiscountCodeValidationResponse

        anon = AnonymousUser()

        response = discount_code_service.preview_discount_code(
            code="SAVE20",
            organization=dc_org,
            tier=dc_paid_tier,
            user=anon,
        )

        assert isinstance(response, DiscountCodeValidationResponse)
        assert response.valid is True
        assert response.discounted_price == Decimal("40.00")

    def test_preview_fixed_amount(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
        dc_fixed: DiscountCode,
    ) -> None:
        """Should return the correct discounted price for FIXED_AMOUNT codes."""
        response = discount_code_service.preview_discount_code(
            code="FLAT10",
            organization=dc_org,
            tier=dc_paid_tier,
            user=dc_buyer,
        )

        assert response.valid is True
        assert response.discount_type == DiscountCode.DiscountType.FIXED_AMOUNT
        assert response.discount_value == Decimal("10.00")
        # 50 - 10 = 40
        assert response.discounted_price == Decimal("40.00")

    def test_preview_invalid_code_raises(
        self,
        dc_org: Organization,
        dc_paid_tier: TicketTier,
        dc_buyer: RevelUser,
    ) -> None:
        """Should raise HttpError for non-existent codes."""
        with pytest.raises(HttpError):
            discount_code_service.preview_discount_code(
                code="INVALID",
                organization=dc_org,
                tier=dc_paid_tier,
                user=dc_buyer,
            )
