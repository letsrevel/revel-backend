"""Staff change-plan endpoint: immediate OFFLINE/FREE swap, tier follows, coded 409 refusals."""

import typing as t
import uuid
from decimal import Decimal

import orjson
import pytest
from django.db.models import Q
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans
from notifications.models import Notification
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db

PM = MembershipSubscriptionPlan.PaymentMethod


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def other_tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Other tier")


def _plan(tier: MembershipTier, name: str, **kw: t.Any) -> MembershipSubscriptionPlan:
    params: dict[str, t.Any] = {"price": Decimal("10.00"), "currency": "EUR", "period_unit": "month"}
    params.update(kw)
    return subscription_plans.create_plan(tier, name=name, **params)


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return _plan(tier, "Monthly")


@pytest.fixture
def target(other_tier: MembershipTier) -> MembershipSubscriptionPlan:
    return _plan(other_tier, "Target", price=Decimal("20.00"))


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="cp_sub", email="cp-sub@example.com", password="pass")


@pytest.fixture
def sub(plan: MembershipSubscriptionPlan, subscriber: RevelUser) -> MembershipSubscription:
    return subscription_lifecycle.create_subscription(plan, subscriber)


def _post(client: Client, org: Organization, sub_id: uuid.UUID, plan_id: uuid.UUID) -> t.Any:
    url = reverse("api:staff_change_subscription_plan", kwargs={"slug": org.slug, "sub_id": sub_id})
    return client.post(url, data=orjson.dumps({"plan_id": str(plan_id)}), content_type="application/json")


def test_offline_cross_tier_happy_path(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    subscriber: RevelUser,
    target: MembershipSubscriptionPlan,
    other_tier: MembershipTier,
    tier: MembershipTier,
) -> None:
    # A gate the member never passed on the destination tier: staff skip it.
    questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    other_tier.membership_questionnaire = org_q
    other_tier.save()
    records_before = MembershipRecord.objects.filter(organization=organization, user=subscriber).count()
    Notification.objects.all().delete()

    response = _post(organization_owner_client, organization, sub.id, target.id)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["id"] == str(sub.id)
    assert body["plan"]["id"] == str(target.id)
    sub.refresh_from_db()
    assert sub.plan_id == target.id
    assert sub.pending_plan_id is None
    assert OrganizationMember.objects.get(organization=organization, user=subscriber).tier_id == other_tier.id
    records = MembershipRecord.objects.filter(organization=organization, user=subscriber)
    assert records.count() == records_before + 1
    assert records.filter(ended_reason=MembershipRecord.EndedReason.TIER_CHANGED).count() == 1
    assert records.filter(valid_until__isnull=True).get().tier_id == other_tier.id
    assert records.filter(Q(tier_id=tier.id), valid_until__isnull=False).count() == 1
    assert not Notification.objects.exists()


def test_free_to_free(
    organization_owner_client: Client,
    organization: Organization,
    subscriber: RevelUser,
    tier: MembershipTier,
    other_tier: MembershipTier,
) -> None:
    free_a = _plan(tier, "Free A", price=Decimal("0"), period_unit="lifetime", payment_method=PM.FREE)
    free_b = _plan(other_tier, "Free B", price=Decimal("0"), period_unit="lifetime", payment_method=PM.FREE)
    free_sub = subscription_lifecycle.create_subscription(free_a, subscriber)

    response = _post(organization_owner_client, organization, free_sub.id, free_b.id)

    assert response.status_code == 200, response.content
    assert OrganizationMember.objects.get(organization=organization, user=subscriber).tier_id == other_tier.id


def _assert_refused(response: t.Any, code: str) -> None:
    assert response.status_code == 409, response.content
    assert response.json()["code"] == code


def test_refuse_online(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    target: MembershipSubscriptionPlan,
) -> None:
    MembershipSubscriptionPlan.objects.filter(pk=sub.plan_id).update(payment_method=PM.ONLINE)
    _assert_refused(_post(organization_owner_client, organization, sub.id, target.id), "plan_change_online")


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_refuse_terminal(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    target: MembershipSubscriptionPlan,
    status: str,
) -> None:
    MembershipSubscription.objects.filter(pk=sub.pk).update(status=status)
    _assert_refused(_post(organization_owner_client, organization, sub.id, target.id), "subscription_terminal")


def test_refuse_paused(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    target: MembershipSubscriptionPlan,
) -> None:
    MembershipSubscription.objects.filter(pk=sub.pk).update(status="paused")
    _assert_refused(_post(organization_owner_client, organization, sub.id, target.id), "subscription_paused")


def test_refuse_unchanged(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    plan: MembershipSubscriptionPlan,
) -> None:
    _assert_refused(_post(organization_owner_client, organization, sub.id, plan.id), "plan_unchanged")


def test_refuse_archived(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    target: MembershipSubscriptionPlan,
) -> None:
    MembershipSubscriptionPlan.objects.filter(pk=target.pk).update(is_active=False)
    _assert_refused(_post(organization_owner_client, organization, sub.id, target.id), "plan_archived")


def test_refuse_payment_method_mismatch(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    other_tier: MembershipTier,
) -> None:
    free = _plan(other_tier, "Free", price=Decimal("0"), period_unit="lifetime", payment_method=PM.FREE)
    _assert_refused(_post(organization_owner_client, organization, sub.id, free.id), "plan_payment_method_mismatch")


def test_refuse_currency_mismatch(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    other_tier: MembershipTier,
) -> None:
    usd = _plan(other_tier, "USD", currency="USD")
    _assert_refused(_post(organization_owner_client, organization, sub.id, usd.id), "plan_currency_mismatch")


def test_sold_out_stays_400(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    other_tier: MembershipTier,
    django_user_model: type[RevelUser],
) -> None:
    capped = _plan(other_tier, "Capped", max_subscriptions=1)
    subscription_lifecycle.create_subscription(
        capped, django_user_model.objects.create_user(username="cp_other", email="cp-o@example.com", password="x")
    )
    response = _post(organization_owner_client, organization, sub.id, capped.id)
    assert response.status_code == 400, response.content


def test_other_org_plan_is_404(
    organization_owner_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    organization_owner_user: RevelUser,
) -> None:
    foreign_org = Organization.objects.create(name="Foreign", slug="foreign-org", owner=organization_owner_user)
    foreign_tier = MembershipTier.objects.create(organization=foreign_org, name="F")
    foreign_plan = _plan(foreign_tier, "Foreign plan")
    assert _post(organization_owner_client, organization, sub.id, foreign_plan.id).status_code == 404


def test_non_staff_forbidden(
    member_client: Client,
    organization: Organization,
    sub: MembershipSubscription,
    target: MembershipSubscriptionPlan,
) -> None:
    assert _post(member_client, organization, sub.id, target.id).status_code == 403
