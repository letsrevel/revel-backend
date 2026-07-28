"""Tests for ``SubscriptionSchema.member_status`` on the admin surfaces.

The admin drawer pre-gates actions that 403 for PAUSED/BANNED members (e.g.
uncancel), so the schema must carry the subscriber's member-row status. List
endpoints serve it from a queryset annotation; single-object responses fall
back to one lookup.
"""

import datetime
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


def _subscription(
    organization: Organization, plan: MembershipSubscriptionPlan, user: RevelUser
) -> MembershipSubscription:
    now = timezone.now()
    return MembershipSubscription.objects.create(
        organization=organization,
        user=user,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )


def _member(
    organization: Organization,
    user: RevelUser,
    tier: MembershipTier,
    status: str,
) -> OrganizationMember:
    return OrganizationMember.objects.update_or_create(
        organization=organization, user=user, defaults={"tier": tier, "status": status}
    )[0]


class TestMemberStatusOnSubscriptionList:
    def test_list_carries_the_member_row_status(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """A staff PAUSE leaves the subscription ACTIVE — the row must say the member is paused."""
        _subscription(organization, plan, member_user)
        _member(organization, member_user, plan.tier, OrganizationMember.MembershipStatus.PAUSED)

        url = reverse("api:list_subscriptions", kwargs={"slug": organization.slug})
        row = organization_owner_client.get(url).json()["results"][0]

        assert row["member_status"] == "paused"

    def test_list_reports_none_when_no_member_row_exists(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        _subscription(organization, plan, member_user)
        OrganizationMember.objects.filter(organization=organization, user=member_user).delete()

        url = reverse("api:list_subscriptions", kwargs={"slug": organization.slug})
        row = organization_owner_client.get(url).json()["results"][0]

        assert row["member_status"] is None

    def test_detail_falls_back_without_the_annotation(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        subscription = _subscription(organization, plan, member_user)
        _member(organization, member_user, plan.tier, OrganizationMember.MembershipStatus.BANNED)

        url = reverse("api:get_subscription", kwargs={"slug": organization.slug, "sub_id": subscription.id})
        body = organization_owner_client.get(url).json()

        assert body["member_status"] == "banned"


class TestMemberStatusOnMembersList:
    def test_nested_subscription_carries_member_status(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """The nested subscription on the members list rides the prefetch annotation."""
        _subscription(organization, plan, member_user)
        _member(organization, member_user, plan.tier, OrganizationMember.MembershipStatus.PAUSED)

        url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
        results = organization_owner_client.get(url).json()["results"]
        row = next(r for r in results if r["user"]["id"] == str(member_user.id))

        assert row["subscription"]["member_status"] == "paused"
