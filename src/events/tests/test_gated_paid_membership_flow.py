"""End-to-end acceptance test for issue #831: a gated AND paid membership tier.

The full applicant journey on a tier that simultaneously has a membership
questionnaire, manual approval, and an active ONLINE plan:

    subscribe (cold) -> blocked on questionnaire
    apply            -> PENDING row
    pass questionnaire -> still gated on approval
    staff approve    -> APPROVED, no member yet
    subscribe        -> Stripe Checkout opens, subscription linked
    activation       -> member ACTIVE, application COMPLETED
    subscribe again  -> refused (already subscribed)

Stripe is mocked at the API-client boundary; the activation step drives
``_ensure_active_member`` — the single funnel both real webhook paths
(``customer.subscription.*`` sync and ``invoice.paid``) go through, whose
webhook wiring is covered by the checkout-webhook test suite.
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
from events.service.subscription_stripe_sync import _ensure_active_member
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


@mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
@mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
def test_gated_paid_membership_full_lifecycle(
    mock_customer: mock.Mock,
    mock_session: mock.Mock,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
    organization: Organization,
) -> None:
    mock_customer.return_value = mock.MagicMock(id="cus_x")
    mock_session.return_value = mock.MagicMock(id="cs_x", url="https://checkout.stripe.com/c/pay/cs_x")

    # --- Arrange: public Stripe-connected org, fully gated + monetized tier.
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    tier = MembershipTier.objects.create(
        organization=organization,
        name="Vetted",
        membership_questionnaire=org_q,
        requires_membership_approval=True,
    )
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=SubscriptionPaymentMethod.ONLINE,
        stripe_product_id="prod_x",
        stripe_price_id="price_x",
    )

    token = RefreshToken.for_user(nonmember_user)
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    subscribe_url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": organization.id})
    apply_url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})

    # --- 1. Cold subscribe: blocked on the questionnaire, no Stripe call.
    response = client.post(subscribe_url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 400, response.content
    assert response.json()["next_step"] == "submit_questionnaire"
    mock_session.assert_not_called()

    # --- 2. Apply with the plan: PENDING row, still asking for the questionnaire.
    response = client.post(
        apply_url, data={"tier_id": str(tier.id), "plan_id": str(plan.id)}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    body = response.json()
    application_id = body["application"]["id"]
    assert body["application"]["status"] == "pending"
    assert body["eligibility"]["next_step"] == "submit_questionnaire"

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
    mock_session.assert_not_called()

    # --- 4. Staff approve: paid branch marks APPROVED, mints no member.
    application = OrganizationMembershipRequest.objects.get(pk=application_id)
    membership_service.approve_membership_request(application, decided_by=organization_owner_user)
    application.refresh_from_db()
    assert application.status == OrganizationMembershipRequest.Status.APPROVED
    assert not OrganizationMember.objects.filter(user=nonmember_user, organization=organization).exists()

    # --- 5. Subscribe: gates pass, Checkout opens, subscription linked.
    response = client.post(subscribe_url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 201, response.content
    assert response.json()["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_x"
    application.refresh_from_db()
    subscription = MembershipSubscription.objects.get(user=nonmember_user, organization=organization)
    assert application.subscription_id == subscription.id
    assert application.status == OrganizationMembershipRequest.Status.APPROVED

    # --- 6. Payment lands (webhook funnel): member ACTIVE, application COMPLETED.
    subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
    subscription.stripe_subscription_id = "sub_x"
    subscription.save(update_fields=["status", "stripe_subscription_id"])
    assert _ensure_active_member(subscription) == "created"
    application.refresh_from_db()
    assert application.status == OrganizationMembershipRequest.Status.COMPLETED
    member = OrganizationMember.objects.get(user=nonmember_user, organization=organization)
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE
    assert member.tier_id == tier.id

    # --- 7. Subscribing again is refused: the membership is already paid for.
    response = client.post(subscribe_url, data={"plan_id": str(plan.id)}, content_type="application/json")
    assert response.status_code == 400, response.content
