"""Tests for discount code service: CRUD operations and price calculations.

Tests cover:
- Creating discount codes with and without M2M scope relations
- Updating discount codes (scalar fields and M2M)
- Price calculation (percentage and fixed amount)
- Discount amount calculation
- Atomic usage counter increment

Validation tests are in test_discount_code_validation.py.
"""

from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    TicketTier,
)
from events.models.discount_code import DiscountCode
from events.schema.discount_code import (
    DiscountCodeCreateSchema,
    DiscountCodeUpdateSchema,
)
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
# create_discount_code
# ===========================================================================


class TestCreateDiscountCode:
    """Tests for the create_discount_code function."""

    def test_create_basic_percentage_code(self, dc_org: Organization) -> None:
        """Should create a percentage discount code with scalar fields only."""
        payload = DiscountCodeCreateSchema(  # type: ignore[call-arg]
            code="SUMMER25",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("25.00"),
        )

        dc = discount_code_service.create_discount_code(dc_org, payload)

        assert dc.code == "SUMMER25"
        assert dc.organization == dc_org
        assert dc.discount_type == DiscountCode.DiscountType.PERCENTAGE
        assert dc.discount_value == Decimal("25.00")
        assert dc.is_active is True
        assert dc.max_uses is None
        assert dc.max_uses_per_user == 1
        assert dc.times_used == 0
        assert dc.min_purchase_amount == Decimal("0")

    def test_create_fixed_amount_code(self, dc_org: Organization) -> None:
        """Should create a fixed amount discount code with currency."""
        payload = DiscountCodeCreateSchema(
            code="FLAT5",
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("5.00"),
            currency="EUR",
            max_uses=100,
            max_uses_per_user=2,
            min_purchase_amount=Decimal("20.00"),
        )

        dc = discount_code_service.create_discount_code(dc_org, payload)

        assert dc.discount_type == DiscountCode.DiscountType.FIXED_AMOUNT
        assert dc.currency == "EUR"
        assert dc.max_uses == 100
        assert dc.max_uses_per_user == 2
        assert dc.min_purchase_amount == Decimal("20.00")

    def test_create_with_m2m_series(self, dc_org: Organization, dc_series: EventSeries) -> None:
        """Should create a discount code scoped to specific series via M2M."""
        payload = DiscountCodeCreateSchema(  # type: ignore[call-arg]
            code="SERIESONLY",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            series_ids=[dc_series.id],
        )

        dc = discount_code_service.create_discount_code(dc_org, payload)

        assert list(dc.series.values_list("id", flat=True)) == [dc_series.id]
        assert dc.events.count() == 0
        assert dc.tiers.count() == 0

    def test_create_with_m2m_events(self, dc_org: Organization, dc_event: Event) -> None:
        """Should create a discount code scoped to specific events via M2M."""
        payload = DiscountCodeCreateSchema(  # type: ignore[call-arg]
            code="EVENTONLY",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("15.00"),
            event_ids=[dc_event.id],
        )

        dc = discount_code_service.create_discount_code(dc_org, payload)

        assert list(dc.events.values_list("id", flat=True)) == [dc_event.id]

    def test_create_with_m2m_tiers(self, dc_org: Organization, dc_paid_tier: TicketTier) -> None:
        """Should create a discount code scoped to specific tiers via M2M."""
        payload = DiscountCodeCreateSchema(  # type: ignore[call-arg]
            code="TIERONLY",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            tier_ids=[dc_paid_tier.id],
        )

        dc = discount_code_service.create_discount_code(dc_org, payload)

        assert list(dc.tiers.values_list("id", flat=True)) == [dc_paid_tier.id]

    def test_create_with_all_m2m(
        self,
        dc_org: Organization,
        dc_series: EventSeries,
        dc_event: Event,
        dc_paid_tier: TicketTier,
    ) -> None:
        """Should set all three M2M relations when provided."""
        payload = DiscountCodeCreateSchema(  # type: ignore[call-arg]
            code="ALLSCOPE",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("30.00"),
            series_ids=[dc_series.id],
            event_ids=[dc_event.id],
            tier_ids=[dc_paid_tier.id],
        )

        dc = discount_code_service.create_discount_code(dc_org, payload)

        assert dc.series.count() == 1
        assert dc.events.count() == 1
        assert dc.tiers.count() == 1


# ===========================================================================
# update_discount_code
# ===========================================================================


class TestUpdateDiscountCode:
    """Tests for the update_discount_code function."""

    def test_update_scalar_field(self, dc_percentage: DiscountCode) -> None:
        """Should update scalar fields via exclude_unset semantics."""
        payload = DiscountCodeUpdateSchema(  # type: ignore[call-arg]
            discount_value=Decimal("30.00"),
        )

        updated = discount_code_service.update_discount_code(dc_percentage, payload)

        assert updated.discount_value == Decimal("30.00")
        # Unchanged fields remain
        assert updated.code == "SAVE20"

    def test_update_is_active(self, dc_percentage: DiscountCode) -> None:
        """Should update the is_active flag."""
        payload = DiscountCodeUpdateSchema(  # type: ignore[call-arg]
            is_active=False,
        )

        updated = discount_code_service.update_discount_code(dc_percentage, payload)

        assert updated.is_active is False

    def test_update_m2m_relations(
        self,
        dc_percentage: DiscountCode,
        dc_paid_tier: TicketTier,
        dc_event: Event,
    ) -> None:
        """Should set M2M relations when provided in the update payload."""
        payload = DiscountCodeUpdateSchema(  # type: ignore[call-arg]
            tier_ids=[dc_paid_tier.id],
            event_ids=[dc_event.id],
        )

        updated = discount_code_service.update_discount_code(dc_percentage, payload)

        assert list(updated.tiers.values_list("id", flat=True)) == [dc_paid_tier.id]
        assert list(updated.events.values_list("id", flat=True)) == [dc_event.id]

    def test_update_clear_m2m(
        self,
        dc_percentage: DiscountCode,
        dc_paid_tier: TicketTier,
    ) -> None:
        """Should clear M2M when an empty list is provided."""
        # First set a tier
        dc_percentage.tiers.add(dc_paid_tier)
        assert dc_percentage.tiers.count() == 1

        payload = DiscountCodeUpdateSchema(  # type: ignore[call-arg]
            tier_ids=[],
        )

        updated = discount_code_service.update_discount_code(dc_percentage, payload)

        assert updated.tiers.count() == 0

    def test_update_no_changes(self, dc_percentage: DiscountCode) -> None:
        """Should handle an empty update gracefully (exclude_unset)."""
        payload = DiscountCodeUpdateSchema()  # type: ignore[call-arg]

        updated = discount_code_service.update_discount_code(dc_percentage, payload)

        assert updated.discount_value == Decimal("20.00")
        assert updated.code == "SAVE20"


# ===========================================================================
# calculate_discounted_price
# ===========================================================================


class TestCalculateDiscountedPrice:
    """Tests for the calculate_discounted_price function."""

    def test_percentage_discount(self, dc_paid_tier: TicketTier, dc_percentage: DiscountCode) -> None:
        """Should calculate percentage discount: 50 * 80% = 40.00."""
        result = discount_code_service.calculate_discounted_price(dc_paid_tier, dc_percentage)

        assert result == Decimal("40.00")

    def test_percentage_100_results_in_zero(self, dc_paid_tier: TicketTier, dc_org: Organization) -> None:
        """Should return 0 for a 100% discount."""
        dc_full = DiscountCode.objects.create(
            code="FREE100",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("100.00"),
            is_active=True,
        )

        result = discount_code_service.calculate_discounted_price(dc_paid_tier, dc_full)

        assert result == Decimal("0.00")

    def test_fixed_amount_discount(self, dc_paid_tier: TicketTier, dc_fixed: DiscountCode) -> None:
        """Should calculate fixed amount discount: 50 - 10 = 40."""
        result = discount_code_service.calculate_discounted_price(dc_paid_tier, dc_fixed)

        assert result == Decimal("40.00")

    def test_fixed_amount_exceeding_price_floors_at_zero(self, dc_paid_tier: TicketTier, dc_org: Organization) -> None:
        """Should return 0 when fixed amount exceeds tier price (never negative)."""
        dc_big = DiscountCode.objects.create(
            code="HUGE",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("999.99"),
            currency="EUR",
            is_active=True,
        )

        result = discount_code_service.calculate_discounted_price(dc_paid_tier, dc_big)

        assert result == Decimal("0")

    def test_small_percentage_rounding(self, dc_paid_tier: TicketTier, dc_org: Organization) -> None:
        """Should round to 2 decimal places for percentage discounts."""
        dc_odd = DiscountCode.objects.create(
            code="ODD",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("33.33"),
            is_active=True,
        )

        result = discount_code_service.calculate_discounted_price(dc_paid_tier, dc_odd)

        # 50 * (100 - 33.33) / 100 = 50 * 66.67 / 100 = 33.335 -> 33.34 (quantize)
        assert result == Decimal("33.34")


# ===========================================================================
# calculate_discount_amount
# ===========================================================================


class TestCalculateDiscountAmount:
    """Tests for the calculate_discount_amount function."""

    def test_percentage_discount_amount(self, dc_paid_tier: TicketTier, dc_percentage: DiscountCode) -> None:
        """Should return the amount subtracted: 50 - 40 = 10."""
        amount = discount_code_service.calculate_discount_amount(dc_paid_tier, dc_percentage)

        assert amount == Decimal("10.00")

    def test_fixed_discount_amount(self, dc_paid_tier: TicketTier, dc_fixed: DiscountCode) -> None:
        """Should return the fixed discount amount: 50 - 40 = 10."""
        amount = discount_code_service.calculate_discount_amount(dc_paid_tier, dc_fixed)

        assert amount == Decimal("10.00")

    def test_discount_amount_capped_at_price(self, dc_paid_tier: TicketTier, dc_org: Organization) -> None:
        """Should cap the discount amount at the tier price (no negative savings)."""
        dc_big = DiscountCode.objects.create(
            code="OVERKILL",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("999.00"),
            currency="EUR",
            is_active=True,
        )

        amount = discount_code_service.calculate_discount_amount(dc_paid_tier, dc_big)

        # tier.price - max(tier.price - 999, 0) = 50 - 0 = 50
        assert amount == Decimal("50.00")


# ===========================================================================
# apply_discount
# ===========================================================================


class TestApplyDiscount:
    """Tests for the apply_discount function."""

    @pytest.fixture
    def dc_unlimited(self, dc_org: Organization) -> DiscountCode:
        """Discount code with high per-user limit for counter tests."""
        return DiscountCode.objects.create(
            code="UNLIMITED",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
            max_uses_per_user=100,
        )

    def test_increments_times_used(self, dc_owner: RevelUser, dc_unlimited: DiscountCode) -> None:
        """Should atomically increment times_used by batch_size."""
        assert dc_unlimited.times_used == 0

        discount_code_service.apply_discount(dc_unlimited, dc_owner, batch_size=1)

        dc_unlimited.refresh_from_db()
        assert dc_unlimited.times_used == 1

    def test_increments_by_batch_size(self, dc_owner: RevelUser, dc_unlimited: DiscountCode) -> None:
        """Should increment by the full batch size, not just 1."""
        discount_code_service.apply_discount(dc_unlimited, dc_owner, batch_size=5)

        dc_unlimited.refresh_from_db()
        assert dc_unlimited.times_used == 5

    def test_multiple_applies_accumulate(self, dc_owner: RevelUser, dc_unlimited: DiscountCode) -> None:
        """Should accumulate across multiple apply calls."""
        discount_code_service.apply_discount(dc_unlimited, dc_owner, batch_size=2)
        discount_code_service.apply_discount(dc_unlimited, dc_owner, batch_size=3)

        dc_unlimited.refresh_from_db()
        assert dc_unlimited.times_used == 5

    def test_rejects_when_global_limit_exceeded(self, dc_owner: RevelUser, dc_org: Organization) -> None:
        """Should reject when global usage limit would be exceeded."""
        dc = DiscountCode.objects.create(
            code="LIMITED",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
            max_uses=2,
            times_used=1,
        )

        with pytest.raises(HttpError):
            discount_code_service.apply_discount(dc, dc_owner, batch_size=2)

    def test_rejects_when_per_user_limit_exceeded(
        self, dc_owner: RevelUser, dc_org: Organization, dc_event: Event, dc_paid_tier: TicketTier
    ) -> None:
        """Should reject when per-user usage limit would be exceeded.

        Simulates the real flow: bulk_create already ran before apply_discount,
        so user_usage includes both old tickets and the current batch's tickets.
        With max_uses_per_user=1, having 2 tickets (1 old + 1 from current batch)
        means user_usage=2 > 1 → reject.
        """
        dc = DiscountCode.objects.create(
            code="PERUSER",
            organization=dc_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
            max_uses_per_user=1,
        )
        from events.models import Ticket

        # Old ticket from a previous purchase
        Ticket.objects.create(
            event=dc_event,
            tier=dc_paid_tier,
            user=dc_owner,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Old",
            discount_code=dc,
        )
        # Current batch ticket (already bulk_created before apply_discount runs)
        Ticket.objects.create(
            event=dc_event,
            tier=dc_paid_tier,
            user=dc_owner,
            status=Ticket.TicketStatus.PENDING,
            guest_name="New",
            discount_code=dc,
        )

        with pytest.raises(HttpError):
            discount_code_service.apply_discount(dc, dc_owner, batch_size=1)
