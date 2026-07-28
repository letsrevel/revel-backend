"""Tests for the member-facing membership payment history endpoint."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
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

pytestmark = pytest.mark.django_db

# The exact member-safe field surface: no raw_response, no platform-fee
# decomposition, no Stripe ids, no staff notes / recorded_by.
EXPECTED_FIELDS = {
    "id",
    "amount",
    "currency",
    "status",
    "period_start",
    "period_end",
    "created_at",
    "refund_amount",
    "refunded_at",
}


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def offline_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """An OFFLINE plan — its members have no Stripe portal at all."""
    return subscription_service.create_plan(
        tier,
        name="Offline monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def subscriber_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="pay_hist_member", email="pay-hist@example.com", password="pass"
    )


@pytest.fixture
def subscriber_client(subscriber_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(subscriber_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


@pytest.fixture
def their_subscription(offline_plan: MembershipSubscriptionPlan, subscriber_user: RevelUser) -> MembershipSubscription:
    return subscription_service.create_subscription(offline_plan, subscriber_user)


def _make_payment(
    subscription: MembershipSubscription,
    *,
    amount: Decimal = Decimal("10.00"),
    status: str = MembershipPayment.PaymentStatus.SUCCEEDED,
    notes: str = "cash at the door",
    refund_amount: Decimal | None = None,
) -> MembershipPayment:
    """Create a payment row against ``subscription``."""
    now = timezone.now()
    return MembershipPayment.objects.create(
        subscription=subscription,
        amount=amount,
        currency="EUR",
        status=status,
        period_start=now,
        period_end=now + timedelta(days=30),
        notes=notes,
        raw_response={"secret": "staff-only"},
        stripe_invoice_id="",
        platform_fee=Decimal("1.00"),
        platform_fee_net=Decimal("0.82"),
        platform_fee_vat=Decimal("0.18"),
        refund_amount=refund_amount,
        refunded_at=now if refund_amount is not None else None,
    )


def _url(organization: Organization) -> str:
    return reverse("api:list_my_membership_payments", kwargs={"org_id": organization.id})


class TestListMyMembershipPayments:
    def test_returns_own_offline_payments_newest_first(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        """OFFLINE (staff-recorded) rows are included — they are the member's only receipt."""
        older = _make_payment(their_subscription, amount=Decimal("10.00"))
        newer = _make_payment(their_subscription, amount=Decimal("12.00"))
        MembershipPayment.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=40))

        response = subscriber_client.get(_url(organization))

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert [row["id"] for row in data["results"]] == [str(newer.id), str(older.id)]
        assert data["results"][0]["amount"] == "12.00"

    def test_never_returns_another_members_payments(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        offline_plan: MembershipSubscriptionPlan,
        organization: Organization,
        nonmember_user: RevelUser,
    ) -> None:
        """Another member's ledger rows in the same org are invisible."""
        mine = _make_payment(their_subscription)
        other_subscription = subscription_service.create_subscription(offline_plan, nonmember_user)
        theirs = _make_payment(other_subscription, amount=Decimal("99.00"))

        response = subscriber_client.get(_url(organization))

        assert response.status_code == 200
        returned = {row["id"] for row in response.json()["results"]}
        assert returned == {str(mine.id)}
        assert str(theirs.id) not in returned

    def test_scoped_to_the_requested_organization(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        subscriber_user: RevelUser,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Payments made in another org do not leak into this org's history."""
        _make_payment(their_subscription)
        other_org = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
        other_tier = MembershipTier.objects.get(organization=other_org, name="General membership")
        other_plan = subscription_service.create_plan(
            other_tier, name="Other", price=Decimal("5.00"), currency="EUR", period_unit="month"
        )
        elsewhere = _make_payment(subscription_service.create_subscription(other_plan, subscriber_user))

        response = subscriber_client.get(_url(organization))

        assert response.status_code == 200
        assert str(elsewhere.id) not in {row["id"] for row in response.json()["results"]}

    def test_includes_history_of_terminated_subscriptions(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        """A cancelled subscription's payments stay visible — it is a billing record."""
        payment = _make_payment(their_subscription)
        their_subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        their_subscription.save(update_fields=["status"])

        response = subscriber_client.get(_url(organization))

        assert response.status_code == 200
        assert [row["id"] for row in response.json()["results"]] == [str(payment.id)]

    def test_field_surface_is_member_safe(
        self,
        subscriber_client: Client,
        their_subscription: MembershipSubscription,
        organization: Organization,
    ) -> None:
        """The row exposes exactly the member-safe fields and nothing else."""
        _make_payment(their_subscription, refund_amount=Decimal("2.50"))

        response = subscriber_client.get(_url(organization))

        assert response.status_code == 200
        row: dict[str, t.Any] = response.json()["results"][0]
        assert set(row) == EXPECTED_FIELDS
        assert row["status"] == MembershipPayment.PaymentStatus.SUCCEEDED
        assert row["refund_amount"] == "2.50"
        assert row["refunded_at"] is not None

    def test_unauthenticated_blocked(self, organization: Organization) -> None:
        response = Client().get(_url(organization))
        assert response.status_code == 401
