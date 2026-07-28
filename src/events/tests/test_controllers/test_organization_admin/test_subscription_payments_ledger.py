"""Tests for the org-wide membership payment ledger endpoint (reconciliation surface)."""

import datetime
import typing as t
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
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


def _set_staff_permission(staff_member: OrganizationStaff, *, manage_subscriptions: bool) -> None:
    perm_map = PermissionMap(manage_subscriptions=manage_subscriptions)
    staff_member.permissions = PermissionsSchema(default=perm_map).model_dump(mode="json")
    staff_member.save(update_fields=["permissions"])


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


def _payment(
    subscription: MembershipSubscription,
    *,
    amount: str = "10.00",
    status: str = MembershipPayment.PaymentStatus.SUCCEEDED,
    stripe_invoice_id: str = "",
    stripe_payment_intent_id: str = "",
) -> MembershipPayment:
    now = timezone.now()
    return MembershipPayment.objects.create(
        subscription=subscription,
        amount=Decimal(amount),
        currency="EUR",
        status=status,
        period_start=now,
        period_end=now + datetime.timedelta(days=30),
        stripe_invoice_id=stripe_invoice_id,
        stripe_payment_intent_id=stripe_payment_intent_id,
    )


def _url(organization: Organization) -> str:
    return reverse("api:list_organization_subscription_payments", kwargs={"slug": organization.slug})


class TestPermissions:
    def test_owner_can_list(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        _payment(_subscription(organization, plan, member_user))
        response = organization_owner_client.get(_url(organization))
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_staff_with_permission_can_list(
        self,
        organization_staff_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        staff_member: OrganizationStaff,
    ) -> None:
        _set_staff_permission(staff_member, manage_subscriptions=True)
        _payment(_subscription(organization, plan, member_user))
        response = organization_staff_client.get(_url(organization))
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_staff_without_permission_blocked(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
    ) -> None:
        _set_staff_permission(staff_member, manage_subscriptions=False)
        response = organization_staff_client.get(_url(organization))
        assert response.status_code == 403

    def test_member_cannot_list(self, member_client: Client, organization: Organization) -> None:
        response = member_client.get(_url(organization))
        assert response.status_code == 403


class TestLedgerContents:
    def test_rows_carry_member_and_plan_identity(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        """The org-wide row must identify who paid and for which plan."""
        subscription = _subscription(organization, plan, member_user)
        _payment(subscription, stripe_invoice_id="in_row", stripe_payment_intent_id="pi_row")

        row = organization_owner_client.get(_url(organization)).json()["results"][0]

        assert row["user_id"] == str(member_user.id)
        assert row["user_email"] == member_user.email
        assert row["user_display_name"] == member_user.get_display_name()
        assert row["plan_id"] == str(plan.id)
        assert row["plan_name"] == "Monthly"
        assert row["subscription_id"] == str(subscription.id)
        assert row["stripe_invoice_id"] == "in_row"
        assert row["stripe_payment_intent_id"] == "pi_row"

    def test_other_org_payments_are_not_listed(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        other_owner = RevelUser.objects.create_user(
            username="ledger_other_owner", email="ledger-other@example.com", password="pass"
        )
        other_org = Organization.objects.create(name="Other Ledger Org", slug="other-ledger", owner=other_owner)
        other_tier = MembershipTier.objects.get(organization=other_org, name="General membership")
        other_plan = MembershipSubscriptionPlan.objects.create(
            tier=other_tier, name="Other", price=Decimal("5.00"), currency="EUR", period_unit="month"
        )
        _payment(_subscription(other_org, other_plan, member_user))
        _payment(_subscription(organization, plan, member_user))

        data = organization_owner_client.get(_url(organization)).json()
        assert data["count"] == 1
        assert data["results"][0]["plan_name"] == "Monthly"

    def test_single_query_regardless_of_page_size(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        public_user: RevelUser,
        django_assert_max_num_queries: t.Any,
    ) -> None:
        """The member/plan joins are select_related — row count must not add queries."""
        one = _subscription(organization, plan, member_user)
        two = _subscription(organization, plan, public_user)
        for subscription in (one, two, one, two):
            _payment(subscription)

        with django_assert_max_num_queries(10) as captured:
            baseline = organization_owner_client.get(_url(organization), {"page_size": 1})
        assert baseline.status_code == 200
        with django_assert_max_num_queries(len(captured.captured_queries)):
            full = organization_owner_client.get(_url(organization), {"page_size": 4})
        assert len(full.json()["results"]) == 4


class TestFiltersAndSearch:
    def test_search_by_stripe_payment_intent(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        public_user: RevelUser,
    ) -> None:
        """The pi_/in_ reverse lookup: a Stripe id finds its ledger row."""
        wanted = _payment(
            _subscription(organization, plan, member_user),
            stripe_payment_intent_id="pi_wanted_123",
            stripe_invoice_id="in_wanted_123",
        )
        _payment(_subscription(organization, plan, public_user), stripe_payment_intent_id="pi_other")

        by_pi = organization_owner_client.get(_url(organization), {"search": "pi_wanted_123"}).json()
        assert [r["id"] for r in by_pi["results"]] == [str(wanted.id)]

        by_invoice = organization_owner_client.get(_url(organization), {"search": "in_wanted_123"}).json()
        assert [r["id"] for r in by_invoice["results"]] == [str(wanted.id)]

    def test_search_by_member_email(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        public_user: RevelUser,
    ) -> None:
        wanted = _payment(_subscription(organization, plan, member_user))
        _payment(_subscription(organization, plan, public_user))

        data = organization_owner_client.get(_url(organization), {"search": member_user.email}).json()
        assert [r["id"] for r in data["results"]] == [str(wanted.id)]

    def test_filter_by_status(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        subscription = _subscription(organization, plan, member_user)
        _payment(subscription)
        refunded = _payment(subscription, status=MembershipPayment.PaymentStatus.REFUNDED)

        data = organization_owner_client.get(_url(organization), {"status": "refunded"}).json()
        assert [r["id"] for r in data["results"]] == [str(refunded.id)]

    def test_filter_by_plan(
        self,
        organization_owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
        public_user: RevelUser,
    ) -> None:
        other_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier, name="Yearly", price=Decimal("100.00"), currency="EUR", period_unit="year"
        )
        # One subscription per user per org, so the two plans need two members.
        _payment(_subscription(organization, plan, member_user))
        wanted = _payment(_subscription(organization, other_plan, public_user))

        data = organization_owner_client.get(_url(organization), {"plan_id": str(other_plan.id)}).json()
        assert [r["id"] for r in data["results"]] == [str(wanted.id)]

    def test_pagination_respects_page_size(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        member_user: RevelUser,
    ) -> None:
        subscription = _subscription(organization, plan, member_user)
        for _ in range(3):
            _payment(subscription)

        data = organization_owner_client.get(_url(organization), {"page_size": 2}).json()
        assert data["count"] == 3
        assert len(data["results"]) == 2
