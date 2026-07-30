"""Tests for the approved-but-unpaid application expiry sweep."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
    SubscriptionPaymentMethod,
)
from events.tasks.subscriptions import APPLICATION_PAYMENT_WINDOW_DAYS, expire_stale_approved_applications

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=SubscriptionPaymentMethod.ONLINE,
    )


def _approved_app(
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan | None,
    age_days: int,
    subscription: MembershipSubscription | None = None,
) -> OrganizationMembershipRequest:
    app = OrganizationMembershipRequest.objects.create(
        user=user,
        organization=organization,
        tier=tier,
        plan=plan,
        subscription=subscription,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    # Bypass auto_now to age the row.
    OrganizationMembershipRequest.objects.filter(pk=app.pk).update(updated_at=timezone.now() - timedelta(days=age_days))
    app.refresh_from_db()
    return app


def test_stale_approved_plan_bearing_application_is_cancelled(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    app = _approved_app(user, organization, tier, plan, age_days=APPLICATION_PAYMENT_WINDOW_DAYS + 1)
    assert expire_stale_approved_applications() == 1
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.CANCELLED
    # The bulk update stamps updated_at (auto_now is bypassed) — the row must
    # record its cancellation time, not the stale approval timestamp.
    assert app.updated_at > timezone.now() - timedelta(minutes=1)


def test_fresh_approved_application_is_untouched(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    app = _approved_app(user, organization, tier, plan, age_days=APPLICATION_PAYMENT_WINDOW_DAYS - 1)
    assert expire_stale_approved_applications() == 0
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.APPROVED


def test_stale_application_with_live_checkout_is_untouched(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    """A non-terminal linked subscription means payment is in flight — leave it."""
    subscription = MembershipSubscription.objects.create(
        user=user, plan=plan, organization=organization, status=MembershipSubscription.SubscriptionStatus.PENDING
    )
    app = _approved_app(
        user, organization, tier, plan, age_days=APPLICATION_PAYMENT_WINDOW_DAYS + 5, subscription=subscription
    )
    assert expire_stale_approved_applications() == 0
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.APPROVED


def test_stale_application_with_dead_checkout_is_cancelled(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    """An expired/cancelled linked subscription does not keep the row alive."""
    subscription = MembershipSubscription.objects.create(
        user=user, plan=plan, organization=organization, status=MembershipSubscription.SubscriptionStatus.CANCELLED
    )
    app = _approved_app(
        user, organization, tier, plan, age_days=APPLICATION_PAYMENT_WINDOW_DAYS + 5, subscription=subscription
    )
    assert expire_stale_approved_applications() == 1
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.CANCELLED


def test_plan_less_approved_application_is_never_expired(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Legacy staff-approval rows carry no payment obligation; age is irrelevant."""
    app = _approved_app(user, organization, tier, plan=None, age_days=APPLICATION_PAYMENT_WINDOW_DAYS * 4)
    assert expire_stale_approved_applications() == 0
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.APPROVED
