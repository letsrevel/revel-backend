"""Offline-paid plans: joining, approval and advance-on-read open an awaiting-payment subscription.

Notifications are captured at the ``notification_requested`` signal inside
``django_capture_on_commit_callbacks(execute=True)`` (pytest-django rolls the wrapping transaction back,
so ``on_commit`` callbacks never fire on their own).
"""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    SubscriptionPaymentMethod,
)
from events.service.organization_service import membership as membership_service
from events.service.subscription import lifecycle as subscription_lifecycle
from notifications.enums import NotificationType
from notifications.signals import notification_requested
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus
Sub = MembershipSubscription.SubscriptionStatus
Omr = OrganizationMembershipRequest.Status


def _client(user: RevelUser) -> Client:
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"  # type: ignore[attr-defined]
    return client


def _post(client: Client, url: str, data: dict[str, t.Any]) -> t.Any:
    return client.post(url, data=data, content_type="application/json")


@pytest.fixture
def sent() -> t.Iterator[list[dict[str, t.Any]]]:
    """Notifications dispatched while the test runs, as ``{"type", "context"}`` dicts."""
    calls: list[dict[str, t.Any]] = []
    with mock.patch.object(
        notification_requested,
        "send",
        side_effect=lambda *a, **kw: calls.append({"type": kw["notification_type"], "context": kw.get("context", {})}),
    ):
        yield calls


def _types(sent: list[dict[str, t.Any]]) -> list[NotificationType]:
    return [c["type"] for c in sent]


@pytest.fixture
def org(organization: Organization) -> Organization:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save()
    return organization


@pytest.fixture
def open_tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=org, name="Open")


@pytest.fixture
def gated_tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=org, name="Vetted", requires_membership_approval=True)


def _offline_plan(tier: MembershipTier, **kw: t.Any) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Annual",
        price=Decimal("50.00"),
        currency="EUR",
        period_unit="year",
        payment_method=SubscriptionPaymentMethod.OFFLINE,
        **kw,
    )


def _urls(org: Organization) -> dict[str, str]:
    return {
        "subscribe": reverse("api:subscribe_to_membership_plan", kwargs={"org_id": org.id}),
        "apply": reverse("api:apply_for_membership", kwargs={"slug": org.slug}),
    }


def _pass_questionnaire(user: RevelUser, questionnaire: Questionnaire) -> None:
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )


def _approve_url(org: Organization, application: OrganizationMembershipRequest) -> str:
    return reverse("api:approve_membership_request", kwargs={"slug": org.slug, "request_id": application.pk})


def _apply(client: Client, org: Organization, tier: MembershipTier, plan: MembershipSubscriptionPlan) -> t.Any:
    return _post(client, _urls(org)["apply"], {"tier_id": str(tier.id), "plan_id": str(plan.id)})


def test_direct_join_creates_awaiting_member_and_pending_sub(
    org: Organization,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=org,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    tier = MembershipTier.objects.create(organization=org, name="Q tier", membership_questionnaire=org_q)
    plan = _offline_plan(tier)
    _pass_questionnaire(nonmember_user, questionnaire)

    with django_capture_on_commit_callbacks(execute=True):
        response = _post(_client(nonmember_user), _urls(org)["subscribe"], {"plan_id": str(plan.id)})

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["checkout_url"] is None
    assert body["subscription"]["status"] == "pending"
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert (member.status, member.tier_id) == (S.AWAITING_PAYMENT, tier.id)
    assert _types(sent) == [NotificationType.MEMBERSHIP_PAYMENT_REQUESTED]


def test_resubscribe_while_awaiting_is_idempotent(
    org: Organization,
    open_tier: MembershipTier,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(open_tier)
    client = _client(nonmember_user)
    url = _urls(org)["subscribe"]

    with django_capture_on_commit_callbacks(execute=True):
        first = _post(client, url, {"plan_id": str(plan.id)})
        second = _post(client, url, {"plan_id": str(plan.id)})

    assert (first.status_code, second.status_code) == (201, 201), second.content
    assert first.json()["subscription"]["id"] == second.json()["subscription"]["id"]
    assert MembershipSubscription.objects.filter(user=nonmember_user).count() == 1
    assert _types(sent) == [NotificationType.MEMBERSHIP_PAYMENT_REQUESTED]


def test_existing_member_keeps_status_and_tier_until_paid(
    org: Organization, open_tier: MembershipTier, gated_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=open_tier, status=S.ACTIVE)
    plan = _offline_plan(open_tier)

    response = _post(_client(nonmember_user), _urls(org)["subscribe"], {"plan_id": str(plan.id)})

    assert response.status_code == 201, response.content
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert (member.status, member.tier_id) == (S.ACTIVE, open_tier.id)


def test_approval_on_apply_then_staff_approve_opens_linked_sub(
    org: Organization,
    gated_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(gated_tier)
    response = _apply(_client(nonmember_user), org, gated_tier, plan)
    assert response.status_code == 201, response.content
    application = OrganizationMembershipRequest.objects.get(pk=response.json()["application"]["id"])
    assert application.status == Omr.PENDING
    sent.clear()

    with django_capture_on_commit_callbacks(execute=True):
        approved = _post(_client(organization_owner_user), _approve_url(org, application), {})

    assert approved.status_code == 204, approved.content
    application.refresh_from_db()
    subscription = MembershipSubscription.objects.get(user=nonmember_user, organization=org)
    assert (application.status, application.subscription_id) == (Omr.APPROVED, subscription.id)
    assert subscription.status == Sub.PENDING
    assert OrganizationMember.objects.get(organization=org, user=nonmember_user).status == S.AWAITING_PAYMENT
    assert _types(sent) == [NotificationType.MEMBERSHIP_PAYMENT_REQUESTED]


def test_reapply_while_awaiting_returns_existing(
    org: Organization,
    gated_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(gated_tier)
    client = _client(nonmember_user)
    application = OrganizationMembershipRequest.objects.get(
        pk=_apply(client, org, gated_tier, plan).json()["application"]["id"]
    )
    membership_service.approve_membership_request(application, organization_owner_user)
    sent.clear()

    with django_capture_on_commit_callbacks(execute=True):
        response = _apply(client, org, gated_tier, plan)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["application"]["id"] == str(application.id)
    assert body["eligibility"]["next_step"] == "wait_for_offline_payment"
    assert OrganizationMembershipRequest.objects.filter(user=nonmember_user).count() == 1
    assert sent == []


def test_reapply_while_awaiting_without_linked_application_is_409(
    org: Organization, open_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    plan = _offline_plan(open_tier)
    client = _client(nonmember_user)
    assert _post(client, _urls(org)["subscribe"], {"plan_id": str(plan.id)}).status_code == 201

    response = _apply(client, org, open_tier, plan)

    assert response.status_code == 409, response.content
    assert not OrganizationMembershipRequest.objects.filter(user=nonmember_user).exists()


def test_staff_approve_on_sold_out_plan_400_stays_pending(
    org: Organization,
    gated_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    django_user_model: type[RevelUser],
) -> None:
    """Staff bypass a paused plan, but the cap is a hard limit: the 400 leaves the application PENDING."""
    plan = _offline_plan(gated_tier, max_subscriptions=1)
    application = OrganizationMembershipRequest.objects.get(
        pk=_apply(_client(nonmember_user), org, gated_tier, plan).json()["application"]["id"]
    )
    other = django_user_model.objects.create_user(username="other", email="o@example.com", password="pw")
    MembershipSubscription.objects.create(user=other, organization=org, plan=plan, status=Sub.PENDING)

    response = _post(_client(organization_owner_user), _approve_url(org, application), {})

    assert response.status_code == 400, response.content
    application.refresh_from_db()
    assert application.status == Omr.PENDING and application.subscription_id is None
    assert not MembershipSubscription.objects.filter(user=nonmember_user).exists()
    assert not OrganizationMember.objects.filter(organization=org, user=nonmember_user).exists()


def test_advance_on_read_returns_wait_verdict(
    org: Organization, open_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """Approval is off yet a PENDING paid application exists (someone used /apply anyway)."""
    plan = _offline_plan(open_tier)
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=org, tier=open_tier, plan=plan
    )

    response = _client(nonmember_user).get(
        reverse("api:get_membership_application", kwargs={"application_id": application.pk})
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["eligibility"]["next_step"] == "wait_for_offline_payment"
    assert body["application"]["status"] == "approved"
    application.refresh_from_db()
    assert application.subscription_id is not None


def test_full_flow_questionnaire_join_pay_active(
    org: Organization,
    nonmember_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=org,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    tier = MembershipTier.objects.create(organization=org, name="Q tier", membership_questionnaire=org_q)
    plan = _offline_plan(tier)
    _pass_questionnaire(nonmember_user, questionnaire)
    subscribed = _post(_client(nonmember_user), _urls(org)["subscribe"], {"plan_id": str(plan.id)})
    subscription = MembershipSubscription.objects.get(pk=subscribed.json()["subscription"]["id"])
    sent.clear()

    with django_capture_on_commit_callbacks(execute=True):
        subscription_lifecycle.record_payment(subscription, amount=Decimal("50.00"), currency="EUR", recorded_by=None)

    subscription.refresh_from_db()
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert (subscription.status, member.status) == (Sub.ACTIVE, S.ACTIVE)
    assert _types(sent).count(NotificationType.MEMBERSHIP_GRANTED) == 1


def test_full_flow_apply_approve_pay_completes_application(
    org: Organization,
    gated_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    plan = _offline_plan(gated_tier)
    application = OrganizationMembershipRequest.objects.get(
        pk=_apply(_client(nonmember_user), org, gated_tier, plan).json()["application"]["id"]
    )
    membership_service.approve_membership_request(application, organization_owner_user)
    subscription = MembershipSubscription.objects.get(user=nonmember_user, organization=org)

    subscription_lifecycle.record_payment(subscription, amount=Decimal("50.00"), currency="EUR", recorded_by=None)

    application.refresh_from_db()
    assert application.status == Omr.COMPLETED
    assert OrganizationMember.objects.get(organization=org, user=nonmember_user).status == S.ACTIVE
    assert MembershipPayment.objects.filter(subscription=subscription).count() == 1


def test_staff_create_without_payment_holds_awaiting_member(
    org: Organization,
    open_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = _offline_plan(open_tier)

    with django_capture_on_commit_callbacks(execute=True):
        response = _post(
            _client(organization_owner_user),
            reverse("api:create_subscription", kwargs={"slug": org.slug}),
            {"plan_id": str(plan.id), "user_id": str(nonmember_user.id)},
        )

    assert response.status_code == 201, response.content
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert (member.status, member.tier_id) == (S.AWAITING_PAYMENT, open_tier.id)
    assert _types(sent) == [NotificationType.MEMBERSHIP_PAYMENT_REQUESTED]


def test_online_paid_approval_sends_approved_notification(
    org: Organization,
    gated_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    sent: list[dict[str, t.Any]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    plan = MembershipSubscriptionPlan.objects.create(
        tier=gated_tier,
        name="Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=SubscriptionPaymentMethod.ONLINE,
    )
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=org, tier=gated_tier, plan=plan
    )

    with django_capture_on_commit_callbacks(execute=True):
        membership_service.approve_membership_request(application, organization_owner_user)

    assert _types(sent) == [NotificationType.MEMBERSHIP_REQUEST_APPROVED]
    assert sent[0]["context"]["frontend_url"].endswith(f"/org/{org.slug}/membership")
    assert not MembershipSubscription.objects.filter(user=nonmember_user).exists()


def test_advance_on_read_skips_user_who_became_member(
    org: Organization, open_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """ALREADY_MEMBER is an allowing verdict, but it must not open a subscription or ask the member to pay."""
    plan = _offline_plan(open_tier)
    application = OrganizationMembershipRequest.objects.create(
        user=nonmember_user, organization=org, tier=open_tier, plan=plan
    )
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=open_tier, status=S.ACTIVE)

    response = _client(nonmember_user).get(
        reverse("api:get_membership_application", kwargs={"application_id": application.pk})
    )

    assert response.status_code == 200, response.content
    assert not MembershipSubscription.objects.filter(user=nonmember_user).exists()


def test_advance_on_read_skips_privileged_user(
    org: Organization, open_tier: MembershipTier, organization_owner_user: RevelUser
) -> None:
    plan = _offline_plan(open_tier)
    application = OrganizationMembershipRequest.objects.create(
        user=organization_owner_user, organization=org, tier=open_tier, plan=plan
    )

    response = _client(organization_owner_user).get(
        reverse("api:get_membership_application", kwargs={"application_id": application.pk})
    )

    assert response.status_code == 200, response.content
    assert not MembershipSubscription.objects.filter(user=organization_owner_user).exists()
