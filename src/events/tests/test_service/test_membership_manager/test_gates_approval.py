"""Tests for ManualApprovalGate."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
)
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, MembershipReasonCode, Reasons
from events.service.membership_manager.gates import ManualApprovalGate

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
def approval_required(organization: Organization) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])


def test_requires_approval_with_no_application_is_apply_able(
    user: RevelUser, organization: Organization, tier: MembershipTier, approval_required: None
) -> None:
    """#787: a user who never applied must reach the Join CTA, not a wait state.

    The gate falls through (so later gates still run) and the final allowed
    verdict carries only ``reason_code=REQUIRES_APPROVAL`` — no prose (there is
    no application to be "awaiting approval") and no ``next_step``.
    """
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    assert ManualApprovalGate(service).check() is None
    assert service.approval_required_annotation is True

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()
    assert result.allowed is True
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.reason is None
    assert result.next_step is None
    assert result.application_id is None


def test_requires_approval_with_pending_application_blocks_with_wait(
    user: RevelUser, organization: Organization, tier: MembershipTier, approval_required: None
) -> None:
    """A PENDING application IS awaiting staff approval — block with the row id."""
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.REQUIRES_APPROVAL)
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.next_step == MembershipNextStep.WAIT_FOR_APPROVAL
    assert result.application_id == app.pk


def test_requires_approval_with_cancelled_application_is_apply_able(
    user: RevelUser, organization: Organization, tier: MembershipTier, approval_required: None
) -> None:
    """A terminal CANCELLED row must not soft-lock the user out of re-applying."""
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.CANCELLED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.reason is None
    assert result.next_step is None


def test_requires_approval_with_plan_still_blocks_on_payment_ready(
    user: RevelUser, organization: Organization, tier: MembershipTier, approval_required: None
) -> None:
    """Regression guard: the fall-through must NOT short-circuit PaymentReadyGate.

    ``PaymentReadyGate`` (gate #10) runs after ``ManualApprovalGate``, so a
    plan-bearing check with no application must still block on PLAN_NOT_ONLINE.
    Converting the fall-through to ``self._allow(...)`` breaks this.
    """
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier, plan=plan)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.PLAN_NOT_ONLINE


def test_approved_application_passes(
    user: RevelUser, organization: Organization, tier: MembershipTier, approval_required: None
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.APPROVED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.reason_code is None
    assert result.next_step is None


def test_tier_override_skips_approval(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])
    tier.requires_membership_approval = False
    tier.save(update_fields=["requires_membership_approval"])
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.reason_code is None
