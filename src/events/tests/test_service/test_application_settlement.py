"""Settlement of the originating application when a subscription activates.

``_ensure_active_member`` is the single funnel both Stripe activation paths go
through (``sync_subscription_from_stripe`` and ``_apply_invoice_outcome``), so
settling there covers checkout-completed and invoice-paid alike. The full
webhook-driven path is exercised by the gated-paid-membership integration test.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    SubscriptionPaymentMethod,
)
from events.service.subscription_stripe_sync import _ensure_active_member

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


@pytest.fixture
def subscription(
    user: RevelUser, organization: Organization, plan: MembershipSubscriptionPlan
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )


def _application(
    subscription: MembershipSubscription, status: str = OrganizationMembershipRequest.Status.APPROVED
) -> OrganizationMembershipRequest:
    return OrganizationMembershipRequest.objects.create(
        user=subscription.user,
        organization=subscription.organization,
        tier=subscription.plan.tier,
        plan=subscription.plan,
        subscription=subscription,
        status=status,
    )


def test_activation_completes_approved_application(subscription: MembershipSubscription) -> None:
    app = _application(subscription)
    stale_updated_at = timezone.now() - datetime.timedelta(days=3)
    OrganizationMembershipRequest.objects.filter(pk=app.pk).update(updated_at=stale_updated_at)

    assert _ensure_active_member(subscription) == "created"
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED
    # The settlement is a bulk .update(), which bypasses auto_now — the
    # timestamp must still be stamped explicitly or "when did this complete?"
    # reads as the approval time.
    assert app.updated_at > stale_updated_at
    assert OrganizationMember.objects.filter(
        user=subscription.user,
        organization=subscription.organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).exists()


def test_activation_completes_pending_application(subscription: MembershipSubscription) -> None:
    """Ungated org: the user applied and paid before any advance ran."""
    app = _application(subscription, status=OrganizationMembershipRequest.Status.PENDING)
    _ensure_active_member(subscription)
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED


def test_activation_without_application_is_noop(subscription: MembershipSubscription) -> None:
    assert _ensure_active_member(subscription) == "created"
    assert not OrganizationMembershipRequest.objects.exists()


def test_activation_leaves_cancelled_application_alone(subscription: MembershipSubscription) -> None:
    app = _application(subscription, status=OrganizationMembershipRequest.Status.CANCELLED)
    _ensure_active_member(subscription)
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.CANCELLED


def test_activation_settles_for_existing_member_too(subscription: MembershipSubscription) -> None:
    """A revival/upgrade ("existing" outcome) still owes the application its settlement."""
    OrganizationMember.objects.create(
        user=subscription.user,
        organization=subscription.organization,
        tier=subscription.plan.tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    app = _application(subscription)
    assert _ensure_active_member(subscription) == "existing"
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED
