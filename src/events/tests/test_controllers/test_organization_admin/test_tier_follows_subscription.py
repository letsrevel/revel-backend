"""Manual tier edits are refused while a live subscription exists (the tier follows the plan)."""

import typing as t
from decimal import Decimal

import orjson
import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans

pytestmark = pytest.mark.django_db

Status = MembershipSubscription.SubscriptionStatus


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def other_tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Other tier")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="tf_sub", email="tf-sub@example.com", password="pass")


@pytest.fixture
def sub(plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> MembershipSubscription:
    return subscription_lifecycle.create_subscription(plan, subscriber)


def _put(client: Client, org: Organization, user: RevelUser, body: dict[str, t.Any]) -> t.Any:
    url = reverse("api:update_organization_member", kwargs={"slug": org.slug, "user_id": user.id})
    return client.put(url, data=orjson.dumps(body), content_type="application/json")


def _member(org: Organization, user: RevelUser) -> OrganizationMember:
    return OrganizationMember.objects.get(organization=org, user=user)


@pytest.mark.parametrize("status", [Status.PENDING, Status.ACTIVE, Status.PAUSED, Status.PAST_DUE])
def test_tier_change_refused_for_every_live_status(
    organization_owner_client: Client,
    organization: Organization,
    subscriber: RevelUser,
    sub: MembershipSubscription,
    tier: MembershipTier,
    other_tier: MembershipTier,
    status: str,
) -> None:
    MembershipSubscription.objects.filter(pk=sub.pk).update(status=status)
    before = _member(organization, subscriber)
    assert before.tier_id == tier.id

    response = _put(organization_owner_client, organization, subscriber, {"tier_id": str(other_tier.id)})

    assert response.status_code == 409, response.content
    assert response.json()["code"] == "tier_follows_subscription"
    assert "detail" in response.json()
    after = _member(organization, subscriber)
    assert after.tier_id == tier.id
    assert after.status == before.status


def test_clear_tier_refused(
    organization_owner_client: Client, organization: Organization, subscriber: RevelUser, sub: MembershipSubscription
) -> None:
    response = _put(organization_owner_client, organization, subscriber, {"tier_id": None})

    assert response.status_code == 409, response.content
    assert response.json()["code"] == "tier_follows_subscription"
    assert _member(organization, subscriber).tier_id is not None


def test_tier_equal_to_plan_tier_allowed(
    organization_owner_client: Client,
    organization: Organization,
    subscriber: RevelUser,
    sub: MembershipSubscription,
    tier: MembershipTier,
) -> None:
    response = _put(organization_owner_client, organization, subscriber, {"tier_id": str(tier.id)})

    assert response.status_code == 200, response.content
    assert response.json()["tier_locked_by_subscription"] is True


def test_status_only_update_unaffected(
    organization_owner_client: Client, organization: Organization, subscriber: RevelUser, sub: MembershipSubscription
) -> None:
    response = _put(organization_owner_client, organization, subscriber, {"status": "paused"})

    assert response.status_code == 200, response.content


def test_no_subscription_allows_tier_change(
    organization_owner_client: Client,
    organization: Organization,
    member_user: RevelUser,
    other_tier: MembershipTier,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user)

    response = _put(organization_owner_client, organization, member_user, {"tier_id": str(other_tier.id)})

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["tier"]["id"] == str(other_tier.id)
    assert body["tier_locked_by_subscription"] is False
    assert body["subscription_id"] is None


@pytest.mark.parametrize("status", [Status.CANCELLED, Status.EXPIRED])
def test_terminal_subscription_allows_tier_change(
    organization_owner_client: Client,
    organization: Organization,
    subscriber: RevelUser,
    sub: MembershipSubscription,
    other_tier: MembershipTier,
    status: str,
) -> None:
    MembershipSubscription.objects.filter(pk=sub.pk).update(status=status)

    response = _put(organization_owner_client, organization, subscriber, {"tier_id": str(other_tier.id)})

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["tier"]["id"] == str(other_tier.id)
    assert body["tier_locked_by_subscription"] is False
    assert body["subscription_id"] is None


def test_flags_on_list_detail_and_put(
    organization_owner_client: Client, organization: Organization, subscriber: RevelUser, sub: MembershipSubscription
) -> None:
    detail = organization_owner_client.get(
        reverse("api:get_organization_member", kwargs={"slug": organization.slug, "user_id": subscriber.id})
    ).json()
    assert detail["tier_locked_by_subscription"] is True
    assert detail["subscription_id"] == str(sub.id)

    rows = organization_owner_client.get(
        reverse("api:list_organization_members", kwargs={"slug": organization.slug})
    ).json()["results"]
    row = next(r for r in rows if r["user"]["id"] == str(subscriber.id))
    assert row["tier_locked_by_subscription"] is True
    assert row["subscription_id"] == str(sub.id)

    put = _put(organization_owner_client, organization, subscriber, {"status": "active"}).json()
    assert put["tier_locked_by_subscription"] is True
    assert put["subscription_id"] == str(sub.id)


def test_list_query_count_flat_as_members_grow(
    organization_owner_client: Client,
    organization: Organization,
    plan: MembershipSubscriptionPlan,
    django_user_model: type[RevelUser],
) -> None:
    url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})

    def _add(n: int) -> None:
        for i in range(n):
            user = django_user_model.objects.create_user(username=f"grow{n}_{i}", email=f"grow{n}_{i}@example.com")
            subscription_lifecycle.create_subscription(plan, user)

    _add(2)
    organization_owner_client.get(url)  # warm caches
    with CaptureQueriesContext(connection) as small:
        organization_owner_client.get(url)
    _add(8)
    with CaptureQueriesContext(connection) as large:
        response = organization_owner_client.get(url)

    assert all(r["tier_locked_by_subscription"] for r in response.json()["results"] if r["subscription_id"])
    assert len(large) == len(small)
