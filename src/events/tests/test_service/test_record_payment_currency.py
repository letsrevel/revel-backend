"""Regression tests: ``record_payment`` refuses a currency the plan doesn't use.

Metrics read the currency off ``plan.currency`` while summing raw payment
amounts, and the revenue report buckets by ``payment.currency`` — a mismatched
row corrupts MRR silently and makes the two reports disagree.
"""

from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service
from events.service.subscription_service import InitialPayment

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    """The default tier auto-created on organization save."""
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """A monthly EUR plan."""
    return subscription_service.create_plan(
        tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who will subscribe."""
    return django_user_model.objects.create_user(
        username="currency_subscriber", email="currency-sub@example.com", password="pass"
    )


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for the organization owner (staff subscription admin)."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


class TestRecordPaymentCurrency:
    def test_mismatched_currency_is_refused(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)

        with pytest.raises(HttpError) as excinfo:
            subscription_service.record_payment(
                sub, amount=Decimal("1200.00"), currency="JPY", recorded_by=organization_owner_user
            )
        assert excinfo.value.status_code == 400
        assert "currency" in str(excinfo.value)

        sub.refresh_from_db()
        assert MembershipPayment.objects.filter(subscription=sub).count() == 0
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_matching_currency_succeeds_case_insensitively(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)

        payment = subscription_service.record_payment(
            sub, amount=Decimal("10.00"), currency="eur", recorded_by=organization_owner_user
        )
        assert payment.pk
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.payments.count() == 1

    def test_initial_payment_currency_mismatch_refuses_creation(
        self,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """The guard also covers the initial payment recorded with a new subscription."""
        with pytest.raises(HttpError) as excinfo:
            subscription_service.create_subscription(
                plan,
                subscriber,
                initial_payment=InitialPayment(
                    amount=Decimal("10.00"), currency="USD", recorded_by=organization_owner_user
                ),
            )
        assert excinfo.value.status_code == 400
        assert not MembershipSubscription.objects.filter(user=subscriber).exists()
        assert MembershipPayment.objects.count() == 0

    def test_endpoint_rejects_mismatched_currency(
        self,
        owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        url = reverse("api:record_subscription_payment", kwargs={"slug": organization.slug, "sub_id": sub.id})

        response = owner_client.post(
            url,
            data=orjson.dumps({"amount": "1200.00", "currency": "JPY"}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "currency" in response.json()["detail"]
        assert MembershipPayment.objects.filter(subscription=sub).count() == 0
