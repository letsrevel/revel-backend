"""Search coverage for the org-admin subscriptions list (Stripe id reverse lookup)."""

import datetime
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
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


def _subscription(
    organization: Organization,
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    *,
    stripe_subscription_id: str | None = None,
    stripe_checkout_session_id: str = "",
) -> MembershipSubscription:
    now = timezone.now()
    return MembershipSubscription.objects.create(
        organization=organization,
        user=user,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
        stripe_subscription_id=stripe_subscription_id,
        stripe_checkout_session_id=stripe_checkout_session_id,
    )


def _url(organization: Organization) -> str:
    return reverse("api:list_subscriptions", kwargs={"slug": organization.slug})


def test_search_by_stripe_subscription_id(
    organization_owner_client: Client,
    organization: Organization,
    plan: MembershipSubscriptionPlan,
    member_user: RevelUser,
    public_user: RevelUser,
) -> None:
    """An organizer holding a sub_... from the Stripe dashboard can resolve the member."""
    wanted = _subscription(organization, plan, member_user, stripe_subscription_id="sub_wanted_123")
    _subscription(organization, plan, public_user, stripe_subscription_id="sub_other_456")

    data = organization_owner_client.get(_url(organization), {"search": "sub_wanted_123"}).json()

    assert [r["id"] for r in data["results"]] == [str(wanted.id)]


def test_search_by_stripe_checkout_session_id(
    organization_owner_client: Client,
    organization: Organization,
    plan: MembershipSubscriptionPlan,
    member_user: RevelUser,
    public_user: RevelUser,
) -> None:
    """A PENDING row only carries a cs_... — that must be searchable too."""
    wanted = _subscription(organization, plan, member_user, stripe_checkout_session_id="cs_wanted_123")
    _subscription(organization, plan, public_user, stripe_checkout_session_id="cs_other_456")

    data = organization_owner_client.get(_url(organization), {"search": "cs_wanted_123"}).json()

    assert [r["id"] for r in data["results"]] == [str(wanted.id)]


def test_search_by_member_email_still_works(
    organization_owner_client: Client,
    organization: Organization,
    plan: MembershipSubscriptionPlan,
    member_user: RevelUser,
    public_user: RevelUser,
) -> None:
    """The pre-existing member-identity search fields are unaffected."""
    wanted = _subscription(organization, plan, member_user, stripe_subscription_id="sub_email_123")
    _subscription(organization, plan, public_user)

    data = organization_owner_client.get(_url(organization), {"search": member_user.email}).json()

    assert [r["id"] for r in data["results"]] == [str(wanted.id)]


def test_search_with_no_match_returns_nothing(
    organization_owner_client: Client,
    organization: Organization,
    plan: MembershipSubscriptionPlan,
    member_user: RevelUser,
) -> None:
    _subscription(organization, plan, member_user, stripe_subscription_id="sub_present_123")

    data = organization_owner_client.get(_url(organization), {"search": "sub_absent_999"}).json()

    assert data["count"] == 0
