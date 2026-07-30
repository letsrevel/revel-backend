"""Tests for PaymentReadyGate (Phase 2: the real pre-payment readiness check).

Plan-bearing checks end in an allowing PROCEED_TO_PAYMENT verdict when the plan
is genuinely payable, and block otherwise: offline plan, org not
Stripe-connected, plan paused or at its sales cap, duplicate non-terminal
subscription, or approval required with no application on file
(next_step=SUBMIT_APPLICATION so the user creates one first).
"""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
    SubscriptionPaymentMethod,
)
from events.service.membership_manager import MembershipEligibility, MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, MembershipReasonCode

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(
        update_fields=[
            "visibility",
            "accept_membership_requests",
            "stripe_account_id",
            "stripe_charges_enabled",
            "stripe_details_submitted",
        ]
    )


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


def _verdict(
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan | None = None,
) -> MembershipEligibility:
    return MembershipEligibilityService(user=user, organization=organization, tier=tier, plan=plan).check_eligibility()


def test_no_plan_falls_through(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()
    assert result.allowed is True
    assert result.next_step is None  # plan-less free application


def test_online_plan_allows_with_proceed_to_payment(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is True
    assert result.next_step == MembershipNextStep.PROCEED_TO_PAYMENT


def test_offline_plan_blocks_plan_not_online(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    plan.payment_method = SubscriptionPaymentMethod.OFFLINE
    plan.save(update_fields=["payment_method"])
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.PLAN_NOT_ONLINE
    assert result.next_step is None


def test_org_not_stripe_connected_blocks(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    organization.stripe_charges_enabled = False
    organization.save(update_fields=["stripe_charges_enabled"])
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.ORG_NOT_STRIPE_CONNECTED


def test_paused_plan_blocks_plan_unavailable(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    plan.sales_status = MembershipSubscriptionPlan.SalesStatus.PAUSED
    plan.save(update_fields=["sales_status"])
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.PLAN_UNAVAILABLE


def test_plan_at_sales_cap_blocks_plan_unavailable(
    user: RevelUser,
    member_user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
) -> None:
    plan.max_subscriptions = 1
    plan.save(update_fields=["max_subscriptions"])
    MembershipSubscription.objects.create(
        user=member_user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.PLAN_UNAVAILABLE


def test_duplicate_non_terminal_subscription_blocks(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.DUPLICATE_ACTIVE_SUBSCRIPTION


def test_approval_required_without_application_blocks_submit_application(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    tier.requires_membership_approval = True
    tier.save(update_fields=["requires_membership_approval"])
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is False
    # No prose: "awaiting staff approval" would be a lie with nothing on file —
    # the code + next_step alone drive the FE (mirrors the free path's shaping).
    assert result.reason is None
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.next_step == MembershipNextStep.SUBMIT_APPLICATION


@pytest.fixture
def free_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Free forever",
        price=Decimal("0"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.LIFETIME,
        payment_method=SubscriptionPaymentMethod.FREE,
    )


def test_free_plan_allows_without_stripe(
    user: RevelUser, organization: Organization, tier: MembershipTier, free_plan: MembershipSubscriptionPlan
) -> None:
    """FREE plans skip both the ONLINE-only block and the Stripe-connected requirement."""
    organization.stripe_account_id = ""
    organization.stripe_charges_enabled = False
    organization.stripe_details_submitted = False
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    result = _verdict(user, organization, tier, free_plan)
    assert result.allowed is True
    assert result.next_step == MembershipNextStep.PROCEED_TO_PAYMENT


def test_free_plan_still_respects_paused_sales(
    user: RevelUser, organization: Organization, tier: MembershipTier, free_plan: MembershipSubscriptionPlan
) -> None:
    free_plan.sales_status = MembershipSubscriptionPlan.SalesStatus.PAUSED
    free_plan.save(update_fields=["sales_status"])
    result = _verdict(user, organization, tier, free_plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.PLAN_UNAVAILABLE


def test_free_plan_still_respects_sales_cap(
    user: RevelUser,
    member_user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    free_plan: MembershipSubscriptionPlan,
) -> None:
    free_plan.max_subscriptions = 1
    free_plan.save(update_fields=["max_subscriptions"])
    MembershipSubscription.objects.create(
        user=member_user,
        plan=free_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    result = _verdict(user, organization, tier, free_plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.PLAN_UNAVAILABLE


def test_free_plan_still_blocks_duplicate_subscription(
    user: RevelUser, organization: Organization, tier: MembershipTier, free_plan: MembershipSubscriptionPlan
) -> None:
    MembershipSubscription.objects.create(
        user=user,
        plan=free_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    result = _verdict(user, organization, tier, free_plan)
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.DUPLICATE_ACTIVE_SUBSCRIPTION


def test_free_plan_still_requires_an_application_when_approval_is_required(
    user: RevelUser, organization: Organization, tier: MembershipTier, free_plan: MembershipSubscriptionPlan
) -> None:
    tier.requires_membership_approval = True
    tier.save(update_fields=["requires_membership_approval"])
    result = _verdict(user, organization, tier, free_plan)
    assert result.allowed is False
    # Same no-prose SUBMIT_APPLICATION contract as the paid path.
    assert result.reason is None
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.next_step == MembershipNextStep.SUBMIT_APPLICATION


def test_approval_required_with_approved_application_allows(
    user: RevelUser, organization: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan
) -> None:
    tier.requires_membership_approval = True
    tier.save(update_fields=["requires_membership_approval"])
    OrganizationMembershipRequest.objects.create(
        user=user,
        organization=organization,
        tier=tier,
        plan=plan,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    result = _verdict(user, organization, tier, plan)
    assert result.allowed is True
    assert result.next_step == MembershipNextStep.PROCEED_TO_PAYMENT
