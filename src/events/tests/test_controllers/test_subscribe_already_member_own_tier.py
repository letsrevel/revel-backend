"""#1518: an ACTIVE member starting a PAID plan at their own tier skips the admission gates; related /apply cases."""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
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
    OrganizationStaff,
)
from events.service.membership_manager import MembershipEligibilityService, advance_application
from events.service.membership_manager.enums import MembershipNextStep
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db

STRIPE_CREATE = "events.service.subscription.stripe.checkout.stripe.checkout.Session.create"
STRIPE_CUSTOMER = "events.service.subscription.stripe.checkout.stripe.Customer.create"
OFFLINE_NOTIFY = "events.service.subscription.offline.dispatch_membership_payment_requested"
Status = OrganizationMember.MembershipStatus


@pytest.fixture
def org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = False  # invite-only
    organization.save()
    return organization


@pytest.fixture
def tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=org, name="General membership")


@pytest.fixture
def other_tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=org, name="Other tier")


def _plan(tier: MembershipTier, method: str) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=f"plan-{method}",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=method,
        stripe_product_id="prod_x",
        stripe_price_id="price_x",
    )


def _client(user: RevelUser) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


def _subscribe(user: RevelUser, org: Organization, plan: MembershipSubscriptionPlan) -> t.Any:
    url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": org.id})
    return _client(user).post(url, data={"plan_id": str(plan.id)}, content_type="application/json")


def _eligibility(user: RevelUser, org: Organization, plan: MembershipSubscriptionPlan) -> dict[str, t.Any]:
    response = _client(user).get(
        reverse("api:get_join_eligibility", kwargs={"slug": org.slug}),
        {"tier_id": str(plan.tier_id), "plan_id": str(plan.id)},
    )
    assert response.status_code == 200, response.content
    return t.cast(dict[str, t.Any], response.json())


def _apply(user: RevelUser, org: Organization, **body: t.Any) -> t.Any:
    return _client(user).post(
        reverse("api:apply_for_membership", kwargs={"slug": org.slug}),
        data={k: str(v) for k, v in body.items()},
        content_type="application/json",
    )


def _member(org: Organization, user: RevelUser, tier: MembershipTier) -> OrganizationMember:
    return OrganizationMember.objects.create(organization=org, user=user, tier=tier, status=Status.ACTIVE)


def test_invite_only_org_active_member_subscribes_offline_at_own_tier(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """AcceptRequestsGate is skipped: preview proceeds, /subscribe opens the PENDING subscription."""
    _member(org, nonmember_user, tier)
    plan = _plan(tier, "offline")

    assert _eligibility(nonmember_user, org, plan)["next_step"] == "proceed_to_payment"
    with mock.patch(OFFLINE_NOTIFY):
        response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 201, response.content
    sub = MembershipSubscription.objects.get(user=nonmember_user, organization=org)
    assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING


def test_non_member_is_still_gated_on_the_same_plan(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """Negative: the skip is for the member only; a non-member on an invite-only org is refused."""
    plan = _plan(tier, "offline")
    assert _eligibility(nonmember_user, org, plan)["next_step"] == "requires_invitation"
    assert _subscribe(nonmember_user, org, plan).status_code != 201


def test_other_tier_active_member_is_still_gated_for_a_paid_plan(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """Negative: a paid plan at ANOTHER tier keeps the admission gates (no special case)."""
    _member(org, nonmember_user, other_tier)
    plan = _plan(tier, "offline")
    assert _eligibility(nonmember_user, org, plan)["next_step"] == "requires_invitation"


def test_approval_required_tier_does_not_ask_an_own_tier_member_for_approval(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """ManualApprovalGate is skipped at the own tier, and still applies at another tier."""
    org.accept_membership_requests = True
    org.save(update_fields=["accept_membership_requests"])
    MembershipTier.objects.filter(pk__in=[tier.pk, other_tier.pk]).update(requires_membership_approval=True)
    _member(org, nonmember_user, tier)

    own = _eligibility(nonmember_user, org, _plan(tier, "offline"))
    other = _eligibility(nonmember_user, org, _plan(other_tier, "offline"))

    assert own["next_step"] == "proceed_to_payment"
    assert other["next_step"] == "submit_application"


def test_questionnaire_with_members_exempt_false_is_not_demanded_at_own_tier(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """MembershipQuestionnaireGate is skipped even when the questionnaire re-gates members."""
    org.accept_membership_requests = True
    q = Questionnaire.objects.create(name="Intake", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=org,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        members_exempt=False,
    )
    org.default_membership_questionnaire = oq
    org.save(update_fields=["accept_membership_requests", "default_membership_questionnaire"])
    _member(org, nonmember_user, tier)
    other_user = RevelUser.objects.create_user(username="other", email="o@example.com", password="x")

    plan = _plan(tier, "offline")

    assert _eligibility(nonmember_user, org, plan)["next_step"] == "proceed_to_payment"
    assert _eligibility(other_user, org, plan)["next_step"] == "submit_questionnaire"


def test_offline_own_tier_subscribe_then_eligibility_awaits_payment(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """After the own-tier start, the PENDING subscription answers awaiting_offline_payment, not already_member."""
    _member(org, nonmember_user, tier)
    plan = _plan(tier, "offline")
    with mock.patch(OFFLINE_NOTIFY):
        assert _subscribe(nonmember_user, org, plan).status_code == 201

    verdict = _eligibility(nonmember_user, org, plan)

    assert verdict["next_step"] == "wait_for_offline_payment"
    assert verdict["reason_code"] == "awaiting_offline_payment"


def test_offline_own_tier_repeat_apply_is_409_awaiting_offline_payment_without_a_new_row(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """A repeat /apply with the plan answers 409 awaiting_offline_payment and creates no application row."""
    _member(org, nonmember_user, tier)
    plan = _plan(tier, "offline")
    with mock.patch(OFFLINE_NOTIFY):
        assert _subscribe(nonmember_user, org, plan).status_code == 201
    rows_before = OrganizationMembershipRequest.objects.filter(user=nonmember_user, organization=org).count()

    response = _apply(nonmember_user, org, tier_id=tier.id, plan_id=plan.id)

    assert response.status_code == 409, response.content
    assert response.json()["reason_code"] == "awaiting_offline_payment"
    assert OrganizationMembershipRequest.objects.filter(user=nonmember_user, organization=org).count() == rows_before


@mock.patch(STRIPE_CREATE)
@mock.patch(STRIPE_CUSTOMER)
def test_online_own_tier_pending_checkout_is_not_already_member(
    customer: mock.Mock, session: mock.Mock, org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """ONLINE: the PENDING checkout answers like for a non-member (duplicate, resumed by /subscribe)."""
    customer.return_value = mock.MagicMock(id="cus_x")
    session.return_value = mock.MagicMock(id="cs_x", url="https://checkout.stripe.com/c/pay/cs_x")
    _member(org, nonmember_user, tier)
    plan = _plan(tier, "online")
    assert _subscribe(nonmember_user, org, plan).status_code == 201

    verdict = _eligibility(nonmember_user, org, plan)
    assert verdict["next_step"] != "already_member"
    assert verdict["reason_code"] == "duplicate_active_subscription"


def test_advancing_a_pending_plan_less_row_at_the_current_tier_completes(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """A PENDING plan-less row at the member's own tier completes idempotently (as before #1518)."""
    _member(org, nonmember_user, tier)
    application = OrganizationMembershipRequest.objects.create(organization=org, user=nonmember_user, tier=tier)

    advanced, _ = advance_application(application)

    assert advanced.status == OrganizationMembershipRequest.Status.COMPLETED


def test_advancing_a_pending_plan_less_row_at_another_tier_stays_pending(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    _member(org, nonmember_user, tier)
    application = OrganizationMembershipRequest.objects.create(organization=org, user=nonmember_user, tier=other_tier)

    advanced, eligibility = advance_application(application)

    assert advanced.status == OrganizationMembershipRequest.Status.PENDING
    assert eligibility.next_step == MembershipNextStep.ALREADY_MEMBER
    assert OrganizationMember.objects.get(organization=org, user=nonmember_user).tier_id == tier.id


@pytest.mark.parametrize("role", ["owner", "staff"])
def test_owner_and_staff_cannot_switch_tier_for_free_via_apply(
    org: Organization,
    tier: MembershipTier,
    other_tier: MembershipTier,
    organization_owner_user: RevelUser,
    organization_staff_user: RevelUser,
    staff_member: OrganizationStaff,
    role: str,
) -> None:
    """The refusal reads the member row, so privileged users get 409 and keep their tier; preview agrees."""
    user = organization_owner_user if role == "owner" else organization_staff_user
    member = _member(org, user, tier)

    response = _apply(user, org, tier_id=other_tier.id)

    assert response.status_code == 409, response.content
    assert response.json()["reason_code"] == "already_active_member"
    member.refresh_from_db()
    assert member.tier_id == tier.id
    assert not OrganizationMembershipRequest.objects.filter(user=user, organization=org).exists()
    verdict = MembershipEligibilityService(user=user, organization=org, tier=other_tier).check_eligibility()
    assert verdict.next_step == MembershipNextStep.ALREADY_MEMBER


def test_active_member_with_pending_online_on_another_plan_is_refused_a_free_plan(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """An abandoned PENDING ONLINE checkout on another plan does not open the door to a FREE plan."""
    _member(org, nonmember_user, tier)
    MembershipSubscription.objects.create(
        user=nonmember_user,
        organization=org,
        plan=_plan(other_tier, "online"),
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_old",
    )
    free = MembershipSubscriptionPlan.objects.create(
        tier=tier, name="free", price=Decimal("0"), currency="EUR", period_unit="lifetime", payment_method="free"
    )

    with mock.patch(STRIPE_CREATE) as session:
        response = _subscribe(nonmember_user, org, free)

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "already_member"
    session.assert_not_called()


def test_stale_rejected_application_does_not_block_an_own_tier_paid_start(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """The REJECTED row is the manual-approval outcome; the own-tier paid start skips it (subscribe + preview)."""
    _member(org, nonmember_user, tier)
    OrganizationMembershipRequest.objects.create(
        organization=org, user=nonmember_user, tier=tier, status=OrganizationMembershipRequest.Status.REJECTED
    )
    plan = _plan(tier, "offline")

    assert _eligibility(nonmember_user, org, plan)["next_step"] == "proceed_to_payment"
    with mock.patch(OFFLINE_NOTIFY):
        assert _subscribe(nonmember_user, org, plan).status_code == 201


def test_non_member_with_rejected_application_still_gets_reapply(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """Negative: the skip is member-only; a non-member keeps the REAPPLY outcome."""
    org.accept_membership_requests = True
    org.save(update_fields=["accept_membership_requests"])
    OrganizationMembershipRequest.objects.create(
        organization=org, user=nonmember_user, tier=tier, status=OrganizationMembershipRequest.Status.REJECTED
    )

    verdict = _eligibility(nonmember_user, org, _plan(tier, "offline"))

    assert verdict["next_step"] == "reapply"
    assert verdict["reason_code"] == "application_rejected"


def _break_stripe(org: Organization, plan: MembershipSubscriptionPlan, other: RevelUser) -> None:
    Organization.objects.filter(pk=org.pk).update(stripe_charges_enabled=False)


def _pause(org: Organization, plan: MembershipSubscriptionPlan, other: RevelUser) -> None:
    MembershipSubscriptionPlan.objects.filter(pk=plan.pk).update(
        sales_status=MembershipSubscriptionPlan.SalesStatus.PAUSED
    )


def _fill_cap(org: Organization, plan: MembershipSubscriptionPlan, other: RevelUser) -> None:
    MembershipSubscriptionPlan.objects.filter(pk=plan.pk).update(max_subscriptions=1)
    MembershipSubscription.objects.create(
        user=other,
        organization=org,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_other",
    )


def _archive(org: Organization, plan: MembershipSubscriptionPlan, other: RevelUser) -> None:
    MembershipSubscriptionPlan.objects.filter(pk=plan.pk).update(is_active=False)


@pytest.mark.parametrize("breaker", [_break_stripe, _pause, _fill_cap, _archive])
def test_own_tier_paid_start_still_refused_where_a_non_member_is_refused(
    breaker: t.Callable[[Organization, MembershipSubscriptionPlan, RevelUser], None],
    org: Organization,
    tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_staff_user: RevelUser,
) -> None:
    """TierAvailability/PaymentReady gates still apply to the own-tier member: same code as a non-member."""
    org.accept_membership_requests = True
    org.save(update_fields=["accept_membership_requests"])
    plan = _plan(tier, "online")
    breaker(org, plan, organization_staff_user)
    member = nonmember_user
    _member(org, member, tier)
    outsider = RevelUser.objects.create_user(username="outsider", email="out@example.com", password="x")

    mine = _eligibility(member, org, plan)
    theirs = _eligibility(outsider, org, plan)

    assert mine["allowed"] is False
    assert mine["reason_code"] == theirs["reason_code"]
    assert mine["next_step"] == theirs["next_step"]
    with mock.patch(STRIPE_CREATE) as session:
        assert _subscribe(member, org, plan).status_code != 201
    session.assert_not_called()


def test_own_tier_paid_start_still_refused_for_a_hard_blacklisted_member(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """BlacklistGate still applies at the own tier."""
    _member(org, nonmember_user, tier)
    plan = _plan(tier, "offline")

    with mock.patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=True):
        verdict = _eligibility(nonmember_user, org, plan)
        with mock.patch(OFFLINE_NOTIFY):
            response = _subscribe(nonmember_user, org, plan)

    assert verdict["allowed"] is False
    assert verdict["reason_code"] == "blacklisted"
    assert response.status_code != 201


def test_payment_ready_does_not_demand_reapproval_for_an_own_tier_member_with_a_closed_subscription(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """The ``not is_own_tier_paid_start`` guard: an APPROVED row linked to a terminal subscription is ignored."""
    MembershipTier.objects.filter(pk=tier.pk).update(requires_membership_approval=True)
    tier.refresh_from_db()
    _member(org, nonmember_user, tier)
    plan = _plan(tier, "offline")
    old = MembershipSubscription.objects.create(
        user=nonmember_user,
        organization=org,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_old",
    )
    # queryset update: a CANCELLED save() would cancel the member row through the subscription signal.
    MembershipSubscription.objects.filter(pk=old.pk).update(status=MembershipSubscription.SubscriptionStatus.CANCELLED)
    OrganizationMembershipRequest.objects.create(
        organization=org,
        user=nonmember_user,
        tier=tier,
        plan=plan,
        status=OrganizationMembershipRequest.Status.APPROVED,
        subscription=old,
    )

    verdict = MembershipEligibilityService(
        user=nonmember_user, organization=org, tier=tier, plan=plan
    ).check_eligibility()

    assert verdict.allowed is True
    assert verdict.next_step == MembershipNextStep.PROCEED_TO_PAYMENT


@pytest.mark.parametrize("method", ["online", "offline"])
def test_advancing_a_pending_paid_own_tier_row_on_an_approval_tier(
    method: str, org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """ONLINE -> APPROVED (awaiting /subscribe); OFFLINE -> the offline subscription is opened."""
    MembershipTier.objects.filter(pk=tier.pk).update(requires_membership_approval=True)
    tier.refresh_from_db()
    _member(org, nonmember_user, tier)
    plan = _plan(tier, method)
    application = OrganizationMembershipRequest.objects.create(
        organization=org, user=nonmember_user, tier=tier, plan=plan
    )

    with mock.patch(OFFLINE_NOTIFY):
        advanced, _ = advance_application(application)

    subs = MembershipSubscription.objects.filter(user=nonmember_user, plan=plan)
    if method == "online":
        assert advanced.status == OrganizationMembershipRequest.Status.APPROVED
        assert not subs.exists()
    else:
        assert subs.count() == 1
        assert subs.get().status == MembershipSubscription.SubscriptionStatus.PENDING
        advanced.refresh_from_db()
        assert advanced.subscription_id == subs.get().pk
