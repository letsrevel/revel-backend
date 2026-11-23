"""#1518: /subscribe refuses an ACTIVE member of the organization with ``already_member``."""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.exceptions import MembershipApprovalRefusalCode, MembershipApprovalRefusedError
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service.membership_manager import MembershipEligibilityService, advance_application
from events.service.membership_manager.enums import MembershipReasonCode
from events.service.organization_service.membership import approve_membership_request

pytestmark = pytest.mark.django_db

STRIPE_CREATE = "events.service.subscription.stripe.checkout.stripe.checkout.Session.create"
STRIPE_CUSTOMER = "events.service.subscription.stripe.checkout.stripe.Customer.create"
Status = OrganizationMember.MembershipStatus


@pytest.fixture
def org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save()
    return organization


@pytest.fixture
def tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=org, name="General membership")


@pytest.fixture
def other_tier(org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=org, name="Other tier")


def _plan(tier: MembershipTier, method: str) -> MembershipSubscriptionPlan:
    free = method == "free"
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=f"plan-{method}",
        price=Decimal("0") if free else Decimal("10.00"),
        currency="EUR",
        period_unit="lifetime" if free else "month",
        payment_method=method,
        stripe_product_id="" if free else "prod_x",
        stripe_price_id="" if free else "price_x",
    )


def _client(user: RevelUser) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


def _subscribe(user: RevelUser, org: Organization, plan: MembershipSubscriptionPlan) -> t.Any:
    url = reverse("api:subscribe_to_membership_plan", kwargs={"org_id": org.id})
    return _client(user).post(url, data={"plan_id": str(plan.id)}, content_type="application/json")


@pytest.mark.parametrize("same_tier", [True, False])
def test_active_member_without_subscription_is_refused_a_free_plan(
    org: Organization,
    tier: MembershipTier,
    other_tier: MembershipTier,
    nonmember_user: RevelUser,
    same_tier: bool,
) -> None:
    """A FREE plan (any tier) 400s ``already_member`` before any write or Stripe call."""
    member_tier = tier if same_tier else other_tier
    member = OrganizationMember.objects.create(
        organization=org, user=nonmember_user, tier=member_tier, status=Status.ACTIVE
    )
    plan = _plan(tier, "free")

    with (
        mock.patch(STRIPE_CREATE) as session,
        mock.patch(STRIPE_CUSTOMER) as customer,
        mock.patch("events.service.subscription.offline.dispatch_membership_payment_requested") as notify,
    ):
        response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 400, response.content
    assert response.json() == {"detail": "You are already a member of this organization.", "code": "already_member"}
    session.assert_not_called()
    customer.assert_not_called()
    notify.assert_not_called()
    member.refresh_from_db()
    assert member.tier_id == member_tier.id
    assert member.status == Status.ACTIVE
    assert not MembershipSubscription.objects.filter(user=nonmember_user, organization=org).exists()


def test_active_owner_member_is_refused_a_free_plan(
    org: Organization, tier: MembershipTier, organization_owner_user: RevelUser
) -> None:
    """Owners pass the gate stack first, so the member row is read directly."""
    OrganizationMember.objects.create(organization=org, user=organization_owner_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(tier, "free")
    response = _subscribe(organization_owner_user, org, plan)
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "already_member"


@pytest.mark.parametrize("same_tier", [True, False])
@mock.patch(STRIPE_CREATE)
@mock.patch(STRIPE_CUSTOMER)
def test_active_member_can_subscribe_online_at_any_tier(
    customer: mock.Mock,
    session: mock.Mock,
    org: Organization,
    tier: MembershipTier,
    other_tier: MembershipTier,
    nonmember_user: RevelUser,
    same_tier: bool,
) -> None:
    """A paid plan is allowed, also at the member's own tier: a checkout session is created, member row untouched."""
    customer.return_value = mock.MagicMock(id="cus_x")
    session.return_value = mock.MagicMock(id="cs_x", url="https://checkout.stripe.com/c/pay/cs_x")
    member_tier = tier if same_tier else other_tier
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=member_tier, status=Status.ACTIVE)
    plan = _plan(tier, "online")

    response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 201, response.content
    assert response.json()["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_x"
    session.assert_called_once()
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert (member.tier_id, member.status) == (member_tier.id, Status.ACTIVE)


def test_error_body_matches_legacy_request_endpoint(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """Same status and body as ``create_membership_request``'s ``AlreadyMemberError``."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, status=Status.ACTIVE)
    plan = _plan(tier, "free")
    legacy = _client(nonmember_user).post(
        reverse("api:create_membership_request", kwargs={"slug": org.slug}),
        data={},
        content_type="application/json",
    )
    assert legacy.status_code == 400, legacy.content
    assert _subscribe(nonmember_user, org, plan).json() == legacy.json()


@pytest.mark.parametrize("status", [Status.CANCELLED, Status.AWAITING_PAYMENT])
def test_non_active_member_can_still_subscribe_free(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser, status: Status
) -> None:
    OrganizationMember.objects.create(organization=org, user=nonmember_user, status=status)
    plan = _plan(tier, "free")

    response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 201, response.content
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert member.status == Status.ACTIVE


def test_non_member_can_subscribe_free(org: Organization, tier: MembershipTier, nonmember_user: RevelUser) -> None:
    plan = _plan(tier, "free")
    assert _subscribe(nonmember_user, org, plan).status_code == 201


def test_existing_subscription_keeps_its_own_error(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """A user who already holds a subscription is not told ``already_member``."""
    plan = _plan(tier, "free")
    assert _subscribe(nonmember_user, org, plan).status_code == 201

    response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 400, response.content
    assert response.json().get("code") != "already_member"


def _subscription(
    org: Organization, user: RevelUser, plan: MembershipSubscriptionPlan, status: str, **extra: t.Any
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(user=user, plan=plan, organization=org, status=status, **extra)


@mock.patch(STRIPE_CREATE)
@mock.patch(STRIPE_CUSTOMER)
@mock.patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.retrieve")
def test_pending_online_checkout_on_same_plan_is_resumed(
    retrieve: mock.Mock,
    customer: mock.Mock,
    session: mock.Mock,
    org: Organization,
    tier: MembershipTier,
    nonmember_user: RevelUser,
) -> None:
    """An ACTIVE same-tier member with a PENDING checkout on THIS plan resumes it, not ``already_member``."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(tier, "online")
    _subscription(
        org,
        nonmember_user,
        plan,
        MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_old",
    )
    retrieve.return_value = mock.MagicMock(
        status="open", url="https://checkout.stripe.com/c/pay/cs_old", created=int(timezone.now().timestamp())
    )

    response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 201, response.content
    assert response.json()["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_old"
    session.assert_not_called()


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_active_member_with_only_a_terminal_subscription_is_refused_a_free_plan(
    org: Organization, tier: MembershipTier, nonmember_user: RevelUser, terminal: str
) -> None:
    """A CANCELLED / EXPIRED subscription is not a live one: the member is still refused a free plan."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(tier, "free")
    _subscription(org, nonmember_user, plan, terminal)
    # The terminal row's signal downgrades the member; restore the ACTIVE row (staff re-activated them).
    OrganizationMember.objects.filter(organization=org, user=nonmember_user).update(tier=tier, status=Status.ACTIVE)

    with mock.patch(STRIPE_CREATE) as session:
        response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "already_member"
    session.assert_not_called()


@pytest.mark.parametrize("same_tier", [True, False])
def test_offline_plan_at_any_tier_keeps_member_tier_and_status(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser, same_tier: bool
) -> None:
    """ADR-0012: an OFFLINE plan at any tier opens a PENDING subscription and leaves the member untouched."""
    member_tier = tier if same_tier else other_tier
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=member_tier, status=Status.ACTIVE)
    plan = _plan(tier, "offline")

    with mock.patch("events.service.subscription.offline.dispatch_membership_payment_requested"):
        response = _subscribe(nonmember_user, org, plan)

    assert response.status_code == 201, response.content
    subscription = MembershipSubscription.objects.get(user=nonmember_user, organization=org)
    assert subscription.status == MembershipSubscription.SubscriptionStatus.PENDING
    member = OrganizationMember.objects.get(organization=org, user=nonmember_user)
    assert (member.tier_id, member.status) == (member_tier.id, Status.ACTIVE)


@pytest.mark.parametrize(
    ("method", "plan_tier_is_member_tier", "expected"),
    [
        ("free", False, "already_member"),
        ("free", True, "already_member"),
        ("online", True, "proceed_to_payment"),
        ("online", False, "proceed_to_payment"),
        ("offline", True, "proceed_to_payment"),
        ("offline", False, "proceed_to_payment"),
    ],
)
def test_join_eligibility_matches_subscribe_for_active_member(
    org: Organization,
    tier: MembershipTier,
    other_tier: MembershipTier,
    nonmember_user: RevelUser,
    method: str,
    plan_tier_is_member_tier: bool,
    expected: str,
) -> None:
    """The preview says ALREADY_MEMBER exactly where ``/subscribe`` refuses (#1518)."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(tier if plan_tier_is_member_tier else other_tier, method)

    response = _client(nonmember_user).get(
        reverse("api:get_join_eligibility", kwargs={"slug": org.slug}),
        {"tier_id": str(plan.tier_id), "plan_id": str(plan.id)},
    )

    assert response.status_code == 200, response.content
    assert response.json()["next_step"] == expected


def test_apply_for_free_plan_at_another_tier_mints_no_application_for_active_member(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(other_tier, "free")

    response = _client(nonmember_user).post(
        reverse("api:apply_for_membership", kwargs={"slug": org.slug}),
        data={"tier_id": str(other_tier.id), "plan_id": str(plan.id)},
        content_type="application/json",
    )

    assert response.status_code == 409, response.content
    assert not OrganizationMembershipRequest.objects.filter(user=nonmember_user, organization=org).exists()


def test_approving_an_old_application_refuses_for_active_member(
    org: Organization,
    tier: MembershipTier,
    other_tier: MembershipTier,
    nonmember_user: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    """An application created before the rule must not be approved into a state ``/subscribe`` refuses."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(other_tier, "free")
    request = OrganizationMembershipRequest.objects.create(
        organization=org, user=nonmember_user, tier=other_tier, plan=plan
    )

    with pytest.raises(MembershipApprovalRefusedError) as exc_info:
        approve_membership_request(request, organization_owner_user)

    assert exc_info.value.code == MembershipApprovalRefusalCode.APPLICANT_ALREADY_MEMBER
    assert exc_info.value.forceable is False
    request.refresh_from_db()
    assert request.status == OrganizationMembershipRequest.Status.PENDING


@pytest.mark.parametrize("kind", ["free_other_tier", "free_own_tier"])
def test_reading_a_stale_application_does_not_approve_it_for_active_member(
    org: Organization,
    tier: MembershipTier,
    other_tier: MembershipTier,
    nonmember_user: RevelUser,
    kind: str,
) -> None:
    """A PENDING application that predates the membership stays PENDING on read: ``/subscribe`` refuses its plan."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    plan = _plan(other_tier if kind == "free_other_tier" else tier, "free")
    application = OrganizationMembershipRequest.objects.create(
        organization=org, user=nonmember_user, tier=plan.tier, plan=plan
    )

    response = _client(nonmember_user).get(
        reverse("api:get_membership_application", kwargs={"application_id": application.id})
    )

    assert response.status_code == 200, response.content
    assert response.json()["eligibility"]["next_step"] == "already_member"
    application.refresh_from_db()
    assert application.status == OrganizationMembershipRequest.Status.PENDING


def test_apply_without_plan_for_another_free_tier_is_refused_for_active_member(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """A plan-less /apply that would switch an ACTIVE member to another tier answers 409 and creates no row."""
    member = OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)

    response = _client(nonmember_user).post(
        reverse("api:apply_for_membership", kwargs={"slug": org.slug}),
        data={"tier_id": str(other_tier.id)},
        content_type="application/json",
    )

    assert response.status_code == 409, response.content
    assert not OrganizationMembershipRequest.objects.filter(user=nonmember_user, organization=org).exists()
    member.refresh_from_db()
    assert member.tier_id == tier.id


def test_advancing_a_pending_plan_less_switch_leaves_it_pending(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """A PENDING plan-less application for another tier, left from before the membership, never switches it."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    application = OrganizationMembershipRequest.objects.create(organization=org, user=nonmember_user, tier=other_tier)

    advanced, eligibility = advance_application(application)

    assert eligibility.next_step == "already_member"
    assert advanced.status == OrganizationMembershipRequest.Status.PENDING
    assert OrganizationMember.objects.get(organization=org, user=nonmember_user).tier_id == tier.id


def test_apply_without_plan_for_another_tier_keeps_duplicate_subscription_answer(
    org: Organization, tier: MembershipTier, other_tier: MembershipTier, nonmember_user: RevelUser
) -> None:
    """A member WITH a live subscription still gets the duplicate-subscription refusal, not already_member."""
    OrganizationMember.objects.create(organization=org, user=nonmember_user, tier=tier, status=Status.ACTIVE)
    _subscription(org, nonmember_user, _plan(tier, "online"), MembershipSubscription.SubscriptionStatus.ACTIVE)

    verdict = MembershipEligibilityService(user=nonmember_user, organization=org, tier=other_tier).check_eligibility()

    assert verdict.allowed is False
    assert verdict.reason_code == MembershipReasonCode.DUPLICATE_ACTIVE_SUBSCRIPTION
