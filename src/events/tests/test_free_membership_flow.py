"""End-to-end tests for the FREE self-serve membership plan (issue #832).

A FREE plan is Stripe-free and member self-serve: ``POST /subscribe`` lands the
subscription ACTIVE immediately with no checkout URL — but only after the tier's
full gate stack (questionnaire, manual approval) has been satisfied, which is
the whole point of the issue cluster.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
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
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

PeriodUnit = MembershipSubscriptionPlan.PeriodUnit


@pytest.fixture
def public_org(organization: Organization) -> Organization:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])
    return organization


@pytest.fixture
def tier(public_org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=public_org, name="Supporters")


@pytest.fixture
def free_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Free forever",
        price=Decimal("0"),
        currency="EUR",
        period_unit=PeriodUnit.LIFETIME,
        payment_method=SubscriptionPaymentMethod.FREE,
    )


def _client(user: RevelUser) -> Client:
    client = Client()
    token = RefreshToken.for_user(user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return client


@mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
def test_subscribe_to_free_plan_activates_immediately(
    mock_session: mock.Mock,
    public_org: Organization,
    free_plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
) -> None:
    client = _client(nonmember_user)
    url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": public_org.id})

    response = client.post(url, data={"plan_id": str(free_plan.id)}, content_type="application/json")

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["checkout_url"] is None
    assert body["subscription"]["status"] == "active"
    assert body["subscription"]["current_period_end"] is None
    mock_session.assert_not_called()

    subscription = MembershipSubscription.objects.get(user=nonmember_user, organization=public_org)
    assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
    assert subscription.current_period_end is None
    assert subscription.stripe_subscription_id is None
    member = OrganizationMember.objects.get(user=nonmember_user, organization=public_org)
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE
    assert member.tier_id == free_plan.tier_id


def test_subscribing_twice_to_a_free_plan_is_refused(
    public_org: Organization,
    free_plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
) -> None:
    client = _client(nonmember_user)
    url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": public_org.id})
    assert client.post(url, data={"plan_id": str(free_plan.id)}, content_type="application/json").status_code == 201

    response = client.post(url, data={"plan_id": str(free_plan.id)}, content_type="application/json")
    assert response.status_code == 400, response.content


@mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
def test_gated_free_membership_full_lifecycle(
    mock_session: mock.Mock,
    public_org: Organization,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    """THE acceptance case: a FREE plan on a questionnaire + approval gated tier.

    cold subscribe -> blocked on questionnaire -> apply -> pass questionnaire ->
    blocked on approval -> staff approve -> subscribe -> member ACTIVE and the
    application settled COMPLETED, with Stripe never involved.
    """
    questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=public_org,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    tier = MembershipTier.objects.create(
        organization=public_org,
        name="Vetted",
        membership_questionnaire=org_q,
        requires_membership_approval=True,
    )
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Free forever",
        price=Decimal("0"),
        currency="EUR",
        period_unit=PeriodUnit.LIFETIME,
        payment_method=SubscriptionPaymentMethod.FREE,
    )

    client = _client(nonmember_user)
    subscribe_url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": public_org.id})
    apply_url = reverse("api:apply_for_membership", kwargs={"slug": public_org.slug})

    # --- 1. Cold subscribe: blocked on the questionnaire.
    response = client.post(subscribe_url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 400, response.content
    assert response.json()["next_step"] == "submit_questionnaire"
    assert not MembershipSubscription.objects.filter(user=nonmember_user).exists()

    # --- 2. Apply with the plan: PENDING row, still gated on the questionnaire.
    response = client.post(
        apply_url, data={"tier_id": str(tier.id), "plan_id": str(plan.id)}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    application_id = response.json()["application"]["id"]

    # --- 3. Pass the questionnaire: now gated on staff approval only.
    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )
    response = client.post(subscribe_url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 400, response.content
    assert response.json()["next_step"] == "wait_for_approval"

    # --- 4. Staff approve: APPROVED, still no member (the plan leg is unpaid-for).
    application = OrganizationMembershipRequest.objects.get(pk=application_id)
    membership_service.approve_membership_request(application, decided_by=organization_owner_user)
    application.refresh_from_db()
    assert application.status == OrganizationMembershipRequest.Status.APPROVED
    assert not OrganizationMember.objects.filter(user=nonmember_user, organization=public_org).exists()

    # --- 5. Subscribe: instant ACTIVE membership, application COMPLETED, no Stripe.
    response = client.post(subscribe_url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 201, response.content
    assert response.json()["checkout_url"] is None
    mock_session.assert_not_called()

    subscription = MembershipSubscription.objects.get(user=nonmember_user, organization=public_org)
    assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
    assert subscription.current_period_end is None
    application.refresh_from_db()
    assert application.subscription_id == subscription.id
    assert application.status == OrganizationMembershipRequest.Status.COMPLETED
    member = OrganizationMember.objects.get(user=nonmember_user, organization=public_org)
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE
    assert member.tier_id == tier.id


def test_free_plan_appears_in_public_listings(
    public_org: Organization,
    free_plan: MembershipSubscriptionPlan,
    nonmember_user: RevelUser,
) -> None:
    client = _client(nonmember_user)

    plans = client.get(reverse("api:list_organization_membership_plans", kwargs={"slug": public_org.slug})).json()
    assert [(p["payment_method"], p["period_unit"], p["price"]) for p in plans] == [("free", "lifetime", "0.00")]

    tiers = client.get(reverse("api:list_organization_membership_tiers", kwargs={"slug": public_org.slug})).json()
    (tier_body,) = [row for row in tiers if row["id"] == str(free_plan.tier_id)]
    # ``is_free`` means "no plan at all, join via /apply" — a FREE *plan* is
    # still a plan, so the tier is not is_free.
    assert tier_body["is_free"] is False
    assert [(p["payment_method"], p["period_unit"]) for p in tier_body["plans"]] == [("free", "lifetime")]
