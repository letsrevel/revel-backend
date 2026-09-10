"""Tests for the staff subscription-plan admin endpoints."""

import typing as t
from decimal import Decimal
from unittest import mock

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans

pytestmark = pytest.mark.django_db


# ---- Fixtures ----


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="ctrl_subscriber", email="ctrl-sub@example.com", password="pass"
    )


def _set_staff_permission(staff_member: OrganizationStaff, *, manage_subscriptions: bool) -> None:
    """Reset staff permission map with manage_subscriptions toggled explicitly."""
    perm_map = PermissionMap(manage_subscriptions=manage_subscriptions)
    staff_member.permissions = PermissionsSchema(default=perm_map).model_dump(mode="json")
    staff_member.save(update_fields=["permissions"])


# ---- Plan endpoints ----


class TestListPlans:
    def test_owner_can_list_plans(
        self,
        organization_owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        url = reverse("api:list_subscription_plans", kwargs={"slug": organization.slug, "tier_id": tier.id})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["tier_id"] == str(tier.id)
        assert data[0]["tier_name"] == tier.name

    def test_member_cannot_list_plans(
        self, member_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        url = reverse("api:list_subscription_plans", kwargs={"slug": organization.slug, "tier_id": tier.id})
        response = member_client.get(url)
        assert response.status_code == 403


class TestListOrganizationPlans:
    def test_owner_lists_plans_across_tiers(
        self,
        organization_owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        other_tier = MembershipTier.objects.create(organization=organization, name="VIP membership")
        other_plan = subscription_plans.create_plan(
            other_tier, name="VIP Monthly", price=Decimal("50.00"), currency="EUR", period_unit="month"
        )

        url = reverse("api:list_organization_plans", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        by_id = {item["id"]: item for item in data}
        assert by_id[str(plan.id)]["tier_name"] == tier.name
        assert by_id[str(other_plan.id)]["tier_name"] == other_tier.name

    def test_is_active_filter(
        self,
        organization_owner_client: Client,
        organization: Organization,
        tier: MembershipTier,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        archived = subscription_plans.create_plan(
            tier, name="Archived", price=Decimal("1.00"), currency="EUR", period_unit="month"
        )
        subscription_plans.archive_plan(archived)

        url = reverse("api:list_organization_plans", kwargs={"slug": organization.slug})

        active_only = organization_owner_client.get(url, {"is_active": "true"})
        assert active_only.status_code == 200
        active_ids = {item["id"] for item in active_only.json()}
        assert str(plan.id) in active_ids
        assert str(archived.id) not in active_ids

        inactive_only = organization_owner_client.get(url, {"is_active": "false"})
        assert inactive_only.status_code == 200
        inactive_ids = {item["id"] for item in inactive_only.json()}
        assert inactive_ids == {str(archived.id)}

    def test_excludes_other_organizations(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        other_owner = RevelUser.objects.create_user(
            username="org_plans_other", email="org-plans-other@example.com", password="pass"
        )
        other_org = Organization.objects.create(name="Other Plans Org", slug="other-plans", owner=other_owner)
        other_tier = MembershipTier.objects.get(organization=other_org, name="General membership")
        subscription_plans.create_plan(
            other_tier, name="Other", price=Decimal("9.00"), currency="EUR", period_unit="month"
        )

        url = reverse("api:list_organization_plans", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert {item["id"] for item in data} == {str(plan.id)}

    def test_member_cannot_list_organization_plans(self, member_client: Client, organization: Organization) -> None:
        url = reverse("api:list_organization_plans", kwargs={"slug": organization.slug})
        response = member_client.get(url)
        assert response.status_code == 403

    def test_staff_without_permission_blocked(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
    ) -> None:
        _set_staff_permission(staff_member, manage_subscriptions=False)
        url = reverse("api:list_organization_plans", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 403


class TestCreatePlan:
    def test_owner_creates_plan(
        self, organization_owner_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": tier.id})
        payload = {
            "name": "Annual",
            "price": "100.00",
            "currency": "EUR",
            "period_unit": "year",
            "period_count": 1,
        }
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Annual"

    def test_staff_with_permission_creates_plan(
        self,
        organization_staff_client: Client,
        organization: Organization,
        tier: MembershipTier,
        staff_member: OrganizationStaff,
    ) -> None:
        _set_staff_permission(staff_member, manage_subscriptions=True)
        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": tier.id})
        payload = {"name": "Monthly", "price": "5.00", "currency": "EUR", "period_unit": "month"}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 201

    def test_staff_without_permission_blocked(
        self,
        organization_staff_client: Client,
        organization: Organization,
        tier: MembershipTier,
        staff_member: OrganizationStaff,
    ) -> None:
        _set_staff_permission(staff_member, manage_subscriptions=False)
        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": tier.id})
        payload = {"name": "Monthly", "price": "5.00", "currency": "EUR", "period_unit": "month"}
        response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403

    def test_unsupported_currency_rejected(
        self, organization_owner_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": tier.id})
        payload = {
            "name": "Monthly",
            "price": "5.00",
            "currency": "ABC",  # not in supported list
            "period_unit": "month",
        }
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 422

    def test_online_plan_without_stripe_connect_rejected(
        self, organization_owner_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        """ONLINE plans need Stripe Connect → 400."""
        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": tier.id})
        payload = {
            "name": "Monthly",
            "price": "5.00",
            "currency": "EUR",
            "period_unit": "month",
            "payment_method": MembershipSubscriptionPlan.PaymentMethod.ONLINE.value,
        }
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 400

    def test_online_plan_without_billing_info_rejected(
        self, organization_owner_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        """ONLINE plans on a fee-bearing org need complete billing info → 422."""
        organization.stripe_account_id = "acct_test_plan_ctrl"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])

        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": tier.id})
        payload = {
            "name": "Monthly",
            "price": "5.00",
            "currency": "EUR",
            "period_unit": "month",
            "payment_method": MembershipSubscriptionPlan.PaymentMethod.ONLINE.value,
        }
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 422


class TestUpdateArchiveDeletePlan:
    def test_patch_plan(
        self, organization_owner_client: Client, organization: Organization, plan: MembershipSubscriptionPlan
    ) -> None:
        url = reverse("api:update_subscription_plan", kwargs={"slug": organization.slug, "plan_id": plan.id})
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"price": "12.00"}), content_type="application/json"
        )
        assert response.status_code == 200
        plan.refresh_from_db()
        assert plan.price == Decimal("12.00")

    def test_archive_plan(
        self, organization_owner_client: Client, organization: Organization, plan: MembershipSubscriptionPlan
    ) -> None:
        url = reverse("api:archive_subscription_plan", kwargs={"slug": organization.slug, "plan_id": plan.id})
        response = organization_owner_client.post(url)
        assert response.status_code == 200
        plan.refresh_from_db()
        assert plan.is_active is False

    def test_delete_plan(
        self, organization_owner_client: Client, organization: Organization, plan: MembershipSubscriptionPlan
    ) -> None:
        url = reverse("api:delete_subscription_plan", kwargs={"slug": organization.slug, "plan_id": plan.id})
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not MembershipSubscriptionPlan.objects.filter(pk=plan.pk).exists()

    def test_delete_plan_blocked_when_subscribed(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        subscription_lifecycle.create_subscription(plan, subscriber)
        url = reverse("api:delete_subscription_plan", kwargs={"slug": organization.slug, "plan_id": plan.id})
        response = organization_owner_client.delete(url)
        assert response.status_code == 400


class TestMigrateSubscribersEndpoint:
    """The migrate-subscribers endpoint queues an async task and returns 202."""

    def test_owner_queues_migration_and_dispatches_task(
        self,
        organization_owner_client: Client,
        organization_owner_user: RevelUser,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        subscription_lifecycle.create_subscription(plan, subscriber)  # one non-terminal subscriber
        url = reverse("api:migrate_plan_subscribers", kwargs={"slug": organization.slug, "plan_id": plan.id})

        with mock.patch("events.tasks.subscriptions.migrate_plan_subscribers.delay") as mock_delay:
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                response = organization_owner_client.post(url)

        assert response.status_code == 202, response.content
        assert response.json() == {"queued": 1}
        # Dispatch is deferred to on_commit, then fires with the plan + staff ids.
        assert len(callbacks) == 1
        mock_delay.assert_called_once_with(str(plan.pk), str(organization_owner_user.pk))

    def test_member_cannot_migrate(
        self,
        member_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
    ) -> None:
        url = reverse("api:migrate_plan_subscribers", kwargs={"slug": organization.slug, "plan_id": plan.id})
        response = member_client.post(url)
        assert response.status_code in (403, 404)
