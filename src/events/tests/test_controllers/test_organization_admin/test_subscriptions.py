"""Tests for the staff subscription admin controller."""

from decimal import Decimal
from uuid import uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

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
from events.service import subscription_service

pytestmark = pytest.mark.django_db


# ---- Fixtures ----


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
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
        self, organization_owner_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        url = reverse("api:list_subscription_plans", kwargs={"slug": organization.slug, "tier_id": tier.id})
        response = organization_owner_client.get(url)
        assert response.status_code == 200

    def test_member_cannot_list_plans(
        self, member_client: Client, organization: Organization, tier: MembershipTier
    ) -> None:
        url = reverse("api:list_subscription_plans", kwargs={"slug": organization.slug, "tier_id": tier.id})
        response = member_client.get(url)
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
        subscription_service.create_subscription(plan, subscriber)
        url = reverse("api:delete_subscription_plan", kwargs={"slug": organization.slug, "plan_id": plan.id})
        response = organization_owner_client.delete(url)
        assert response.status_code == 400


# ---- Subscription endpoints ----


class TestSubscriptionEndpoints:
    def test_list_subscriptions(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        subscription_service.create_subscription(plan, subscriber)
        url = reverse("api:list_subscriptions", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_get_subscription(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        url = reverse("api:get_subscription", kwargs={"slug": organization.slug, "sub_id": sub.id})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        assert response.json()["id"] == str(sub.id)

    def test_create_subscription_with_initial_payment(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        url = reverse("api:create_subscription", kwargs={"slug": organization.slug})
        payload = {
            "plan_id": str(plan.id),
            "user_id": str(subscriber.id),
            "initial_payment_amount": "10.00",
            "initial_payment_currency": "EUR",
            "initial_payment_notes": "manual cash",
        }
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 201
        sub = MembershipSubscription.objects.get(user=subscriber)
        assert sub.payments.count() == 1

    def test_create_subscription_initial_amount_without_currency_rejected(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        url = reverse("api:create_subscription", kwargs={"slug": organization.slug})
        payload = {
            "plan_id": str(plan.id),
            "user_id": str(subscriber.id),
            "initial_payment_amount": "10.00",
        }
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 400

    def test_record_payment(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)
        url = reverse("api:record_subscription_payment", kwargs={"slug": organization.slug, "sub_id": sub.id})
        payload = {"amount": "10.00", "currency": "EUR"}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 201
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_cancel_pause_resume(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(plan, subscriber)

        pause_url = reverse("api:pause_subscription", kwargs={"slug": organization.slug, "sub_id": sub.id})
        assert organization_owner_client.post(pause_url).status_code == 200
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PAUSED

        resume_url = reverse("api:resume_subscription", kwargs={"slug": organization.slug, "sub_id": sub.id})
        assert organization_owner_client.post(resume_url).status_code == 200
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

        cancel_url = reverse("api:cancel_subscription", kwargs={"slug": organization.slug, "sub_id": sub.id})
        response = organization_owner_client.post(
            cancel_url, data=orjson.dumps({"immediate": True}), content_type="application/json"
        )
        assert response.status_code == 200
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.CANCELLED

    def test_refund_payment(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        sub = subscription_service.create_subscription(
            plan,
            subscriber,
            initial_payment=subscription_service.InitialPayment(
                amount=Decimal("10.00"), currency="EUR", recorded_by=organization_owner_user
            ),
        )
        payment = sub.payments.first()
        assert payment is not None
        url = reverse(
            "api:refund_subscription_payment",
            kwargs={"slug": organization.slug, "payment_id": payment.id},
        )
        response = organization_owner_client.post(
            url, data=orjson.dumps({"notes": "refund test"}), content_type="application/json"
        )
        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == MembershipPayment.PaymentStatus.REFUNDED

    def test_member_cannot_create_subscription(
        self,
        member_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        url = reverse("api:create_subscription", kwargs={"slug": organization.slug})
        payload = {"plan_id": str(plan.id), "user_id": str(subscriber.id)}
        response = member_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403


class TestCrossOrgIsolation:
    def test_cannot_act_on_other_org_plan(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        other_owner = RevelUser.objects.create_user(username="cross_owner", email="cross@example.com", password="pass")
        other_org = Organization.objects.create(name="Other Org", slug="other", owner=other_owner)
        other_tier = MembershipTier.objects.get(organization=other_org, name="General membership")
        other_plan = subscription_service.create_plan(
            other_tier, name="Monthly", price=Decimal("5.00"), currency="EUR", period_unit="month"
        )

        url = reverse(
            "api:update_subscription_plan",
            kwargs={"slug": organization.slug, "plan_id": other_plan.id},
        )
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"price": "1.00"}), content_type="application/json"
        )
        assert response.status_code == 404

    def test_missing_plan_returns_404(self, organization_owner_client: Client, organization: Organization) -> None:
        url = reverse(
            "api:update_subscription_plan",
            kwargs={"slug": organization.slug, "plan_id": uuid4()},
        )
        response = organization_owner_client.patch(
            url, data=orjson.dumps({"price": "1.00"}), content_type="application/json"
        )
        assert response.status_code == 404
