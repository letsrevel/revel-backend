"""Tests for the subscription inlined on the org-admin member rows.

Ban / blacklist / remove-member cancel the member's non-terminal subscription and
stop Stripe billing, so the admin confirmation dialogs need to know whether one
exists. See issue #805.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
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
        tier=tier,
        name="Gold Annual",
        price=Decimal("120.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.YEAR,
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
    )


def _subscribe(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    organization: Organization,
    status: str = MembershipSubscription.SubscriptionStatus.ACTIVE,
) -> MembershipSubscription:
    now = timezone.now()
    return MembershipSubscription.objects.create(
        user=user,
        organization=organization,
        plan=plan,
        status=status,
        current_period_start=now,
        current_period_end=now + timedelta(days=365),
    )


def _member_row(client: Client, organization: Organization, user: RevelUser) -> dict[str, t.Any]:
    url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
    response = client.get(url)
    assert response.status_code == 200, response.content
    rows = [row for row in response.json()["results"] if row["user"]["id"] == str(user.id)]
    assert len(rows) == 1
    return t.cast(dict[str, t.Any], rows[0])


class TestListMembersSubscription:
    def test_non_terminal_subscription_is_inlined(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        tier: MembershipTier,
    ) -> None:
        """A live subscription is inlined with the plan detail the ban dialog needs."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        subscription = _subscribe(plan, member_user, organization)

        row = _member_row(organization_owner_client, organization, member_user)

        assert row["subscription"] is not None
        assert row["subscription"]["id"] == str(subscription.id)
        assert row["subscription"]["status"] == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert row["subscription"]["current_period_end"] is not None
        assert row["subscription"]["user_id"] == str(member_user.id)
        assert row["subscription"]["plan"]["name"] == "Gold Annual"
        assert row["subscription"]["plan"]["price"] == "120.00"
        assert row["subscription"]["plan"]["currency"] == "EUR"
        assert row["subscription"]["plan"]["period_unit"] == MembershipSubscriptionPlan.PeriodUnit.YEAR
        assert row["subscription"]["plan"]["tier_name"] == tier.name

    def test_member_without_subscription_is_null(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """A legacy (unpaid) membership carries no subscription."""
        OrganizationMember.objects.create(organization=organization, user=member_user)

        assert _member_row(organization_owner_client, organization, member_user)["subscription"] is None

    @pytest.mark.parametrize(
        "status",
        [
            MembershipSubscription.SubscriptionStatus.CANCELLED,
            MembershipSubscription.SubscriptionStatus.EXPIRED,
        ],
    )
    def test_terminal_subscription_is_not_inlined(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        status: str,
    ) -> None:
        """Terminal history must not make the dialog warn about billing that already stopped."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        _subscribe(plan, member_user, organization, status=status)

        assert _member_row(organization_owner_client, organization, member_user)["subscription"] is None

    def test_subscription_from_another_organization_is_not_inlined(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        member_user: RevelUser,
    ) -> None:
        """The inlined subscription is scoped to the organization being administered."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org-805", owner=organization_owner_user)
        other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")
        other_plan = MembershipSubscriptionPlan.objects.create(
            tier=other_tier, name="Elsewhere", price=Decimal("5.00"), currency="EUR"
        )
        OrganizationMember.objects.create(organization=organization, user=member_user)
        _subscribe(other_plan, member_user, other_org)

        assert _member_row(organization_owner_client, organization, member_user)["subscription"] is None

    def test_subscribers_cost_no_extra_queries(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        django_user_model: type[RevelUser],
        django_assert_num_queries: t.Any,
    ) -> None:
        """The inlined subscription is prefetched — more subscribers must not add queries."""
        url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
        OrganizationMember.objects.create(organization=organization, user=member_user)
        _subscribe(plan, member_user, organization)

        with CaptureQueriesContext(connection) as captured:
            assert organization_owner_client.get(url).status_code == 200
        one_subscriber = len(captured.captured_queries)

        for i in range(3):
            user = django_user_model.objects.create_user(
                username=f"sub805_{i}", email=f"sub805-{i}@example.com", password="pass"
            )
            OrganizationMember.objects.create(organization=organization, user=user)
            _subscribe(plan, user, organization)

        with django_assert_num_queries(one_subscriber):
            response = organization_owner_client.get(url)
        assert response.json()["count"] == 4
        assert all(row["subscription"] is not None for row in response.json()["results"])


class TestSingleMemberSubscription:
    def test_update_member_inlines_subscription(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        """The single-member endpoints have no prefetch and fall back to a lookup."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        subscription = _subscribe(plan, member_user, organization)

        url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
        response = organization_owner_client.put(
            url, data=orjson.dumps({"status": "paused"}), content_type="application/json"
        )

        assert response.status_code == 200, response.content
        assert response.json()["subscription"]["id"] == str(subscription.id)

    def test_add_member_without_subscription_is_null(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Adding a member with no subscription resolves to null, not an error."""
        tier = MembershipTier.objects.create(organization=organization, name="Gold 805")
        url = reverse(
            "api:create_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id}
        )
        response = organization_owner_client.post(
            url, data=orjson.dumps({"tier_id": str(tier.id)}), content_type="application/json"
        )

        assert response.status_code == 201, response.content
        assert response.json()["subscription"] is None
