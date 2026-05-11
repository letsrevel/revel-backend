"""Tests for the MembershipSubscription -> OrganizationMember sync signal."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def pro_tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="sub_signal", email="signal@example.com", password="pass")


class TestSyncMemberFromSubscription:
    def test_does_not_create_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        """Creating a subscription directly (bypassing service) must not create a member via signal."""
        sub = MembershipSubscription.objects.create(user=subscriber, plan=plan, organization=organization)
        # The signal fires on save but only updates existing members.
        assert not OrganizationMember.objects.filter(organization=organization, user=subscriber).exists()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_active_member_unchanged_when_status_already_matches(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.ACTIVE,
            tier=tier,
        )
        subscription_service.create_subscription(plan, subscriber)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_subscription_tier_wins(
        self,
        organization: Organization,
        subscriber: RevelUser,
        pro_tier: MembershipTier,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            tier=MembershipTier.objects.get(organization=organization, name="General membership"),
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        pro_plan = subscription_service.create_plan(
            pro_tier, name="Pro Monthly", price=Decimal("20.00"), currency="EUR", period_unit="month"
        )
        subscription_service.create_subscription(pro_plan, subscriber)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.tier_id == pro_tier.pk

    def test_banned_member_is_never_overwritten(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization,
            user=subscriber,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        # Bypass service refusal: directly create the subscription row so we can
        # confirm the signal alone does not overwrite BANNED.
        sub = MembershipSubscription.objects.create(user=subscriber, plan=plan, organization=organization)
        sub.status = MembershipSubscription.SubscriptionStatus.ACTIVE
        sub.save()
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.BANNED

    def test_expired_subscription_cancels_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        sub.save()
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.CANCELLED

    def test_paused_subscription_pauses_member(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.pause_subscription(sub)
        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.PAUSED

    def test_stale_terminal_subscription_does_not_overwrite_active(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        """Re-saving an older terminal subscription must not clobber the current one.

        Scenario: user subscribes (Sub1), cancels, then resubscribes (Sub2).
        Sub1 is later re-saved (e.g. via admin edit). The signal must not
        flip the member back to CANCELLED because Sub2 owns the state.
        """
        old_sub = subscription_service.create_subscription(plan, subscriber)
        subscription_service.cancel_subscription(old_sub, immediate=True)
        subscription_service.create_subscription(plan, subscriber)

        member = OrganizationMember.objects.get(organization=organization, user=subscriber)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

        # Re-saving the old (terminal) subscription must NOT touch the member.
        # Refresh first so the local copy reflects the CANCELLED state — otherwise
        # the stale PENDING status would violate the partial-unique constraint.
        old_sub.refresh_from_db()
        old_sub.save()
        member.refresh_from_db()
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE


class TestModelIntegrity:
    def test_subscription_org_must_match_plan_org(
        self,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization_owner_user: RevelUser,
    ) -> None:
        other_org = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
        sub = MembershipSubscription(user=subscriber, plan=plan, organization=other_org)
        with pytest.raises(ValidationError):
            sub.save()
