"""Tests for POST /events/{event_id}/tickets/{tier_id}/validate-discount endpoint.

Tests cover:
- Valid discount code returns preview for authenticated user
- Valid discount code returns preview for anonymous (unauthenticated) user
- Invalid discount code returns valid=False with a message
- Non-existent code returns valid=False
- Free tier rejection returns valid=False
- Expired code returns valid=False
"""

from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.discount_code import DiscountCode

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dc_event(organization: Organization) -> Event:
    """A public event for discount validation tests."""
    return Event.objects.create(
        organization=organization,
        name="DC Validate Event",
        slug="dc-validate-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        start=timezone.now() + timedelta(days=7),
        status="open",
        requires_ticket=True,
    )


@pytest.fixture
def dc_paid_tier(dc_event: Event) -> TicketTier:
    """A paid tier (online, fixed, EUR) for the discount validation event."""
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
    """A free tier for the discount validation event."""
    return TicketTier.objects.create(
        event=dc_event,
        name="Free Tier",
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def dc_active_code(organization: Organization) -> DiscountCode:
    """An active 20% discount code for the test organization."""
    return DiscountCode.objects.create(
        code="VALID20",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("20.00"),
        is_active=True,
    )


@pytest.fixture
def dc_expired_code(organization: Organization) -> DiscountCode:
    """An expired discount code."""
    return DiscountCode.objects.create(
        code="OLDCODE",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        valid_until=timezone.now() - timedelta(days=1),
        is_active=True,
    )


@pytest.fixture
def authenticated_client(nonmember_user: RevelUser) -> Client:
    """An authenticated client for a non-member user."""
    refresh = RefreshToken.for_user(nonmember_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def anonymous_client() -> Client:
    """An anonymous (unauthenticated) client."""
    return Client()


# ===========================================================================
# Validate discount code (public endpoint)
# ===========================================================================


class TestValidateDiscountEndpoint:
    """Tests for the validate-discount public endpoint."""

    def test_valid_code_authenticated_user(
        self,
        authenticated_client: Client,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_active_code: DiscountCode,
    ) -> None:
        """Should return valid=True with discount details for an authenticated user."""
        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_paid_tier.pk},
        )
        payload = {"code": "VALID20"}

        response = authenticated_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["discount_type"] == "percentage"
        assert data["discount_value"] == "20.00"
        # 50 * 0.80 = 40.00
        assert data["discounted_price"] == "40.00"
        assert data["message"] is None

    def test_valid_code_anonymous_user(
        self,
        anonymous_client: Client,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_active_code: DiscountCode,
    ) -> None:
        """Should return valid=True for an anonymous (unauthenticated) user."""
        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_paid_tier.pk},
        )
        payload = {"code": "VALID20"}

        response = anonymous_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["discounted_price"] == "40.00"

    def test_nonexistent_code_returns_invalid(
        self,
        authenticated_client: Client,
        dc_event: Event,
        dc_paid_tier: TicketTier,
    ) -> None:
        """Should return valid=False for a non-existent code."""
        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_paid_tier.pk},
        )
        payload = {"code": "DOESNOTEXIST"}

        response = authenticated_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = orjson.loads(response.content)
        assert data["valid"] is False

    def test_expired_code_returns_invalid(
        self,
        authenticated_client: Client,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_expired_code: DiscountCode,
    ) -> None:
        """Should return valid=False with a message for an expired code."""
        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_paid_tier.pk},
        )
        payload = {"code": "OLDCODE"}

        response = authenticated_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert data["message"] is not None
        assert "expired" in data["message"]

    def test_free_tier_returns_invalid(
        self,
        authenticated_client: Client,
        dc_event: Event,
        dc_free_tier: TicketTier,
        dc_active_code: DiscountCode,
    ) -> None:
        """Should return valid=False when trying to apply discount to a free tier."""
        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_free_tier.pk},
        )
        payload = {"code": "VALID20"}

        response = authenticated_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert "free" in data["message"].lower()

    def test_case_insensitive_code(
        self,
        authenticated_client: Client,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_active_code: DiscountCode,
    ) -> None:
        """Should find the code regardless of input case."""
        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_paid_tier.pk},
        )
        payload = {"code": "valid20"}

        response = authenticated_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True

    def test_inactive_code_returns_invalid(
        self,
        authenticated_client: Client,
        dc_event: Event,
        dc_paid_tier: TicketTier,
        dc_active_code: DiscountCode,
    ) -> None:
        """Should return valid=False when the code is inactive."""
        dc_active_code.is_active = False
        dc_active_code.save(update_fields=["is_active"])

        url = reverse(
            "api:validate_discount_code",
            kwargs={"event_id": dc_event.pk, "tier_id": dc_paid_tier.pk},
        )
        payload = {"code": "VALID20"}

        response = authenticated_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = orjson.loads(response.content)
        assert data["valid"] is False
