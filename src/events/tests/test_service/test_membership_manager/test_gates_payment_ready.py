"""Tests for PaymentReadyGate.

Phase 1 keeps it conservative: a no-op when plan=None, and a hard block for
any plan-bearing application (``MembershipSubscriptionPlan`` has no
``payment_method`` until Phase 2, so no plan can be paid for online). The
ONLINE-path tests (PROCEED_TO_PAYMENT, Stripe-connect readiness, duplicate
subscription) return with Phase 2 on dev/subscriptions.
"""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import Reasons

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
    )


def test_no_plan_falls_through(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.next_step is None  # plan-less free application


def test_plan_blocks_until_online_payments_ship(
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    plan: MembershipSubscriptionPlan,
) -> None:
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier, plan=plan)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.PLAN_NOT_ONLINE)
