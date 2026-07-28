"""Tests for clearing a scheduled cancellation (issue #808).

Covers the member-facing and staff-facing ``uncancel`` endpoints plus the
shared service guards.
"""

from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.test.client import Client
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_service, subscription_uncancel

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_service.create_plan(
        tier, name="Monthly", price=Decimal("10.00"), currency="EUR", period_unit="month"
    )


@pytest.fixture
def online_plan(organization: Organization, tier: MembershipTier) -> MembershipSubscriptionPlan:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(
        update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"],
    )
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_uncancel",
        stripe_price_id="price_uncancel",
    )


@pytest.fixture
def subscriber_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="uncancel_sub", email="uncancel-sub@example.com", password="pass"
    )


@pytest.fixture
def subscriber_client(subscriber_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(subscriber_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


def _scheduled_cancel(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    organization: Organization,
    *,
    stripe_subscription_id: str = "",
) -> MembershipSubscription:
    """An ACTIVE subscription already scheduled to cancel at period end."""
    subscription = MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        cancel_at_period_end=True,
        stripe_subscription_id=stripe_subscription_id,
    )
    return subscription


def _member_url(organization: Organization) -> str:
    return reverse("api:uncancel_my_membership_subscription", kwargs={"org_id": organization.id})


def _admin_url(organization: Organization, subscription: MembershipSubscription) -> str:
    return reverse("api:uncancel_subscription", kwargs={"slug": organization.slug, "sub_id": subscription.id})


class TestMemberUncancel:
    def test_member_clears_scheduled_cancellation(
        self,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 200, response.content
        assert response.json()["cancel_at_period_end"] is False
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is False
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    @mock.patch("events.service.subscription_uncancel.stripe.Subscription.modify")
    def test_online_clears_the_flag_on_stripe(
        self,
        mock_modify: mock.Mock,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(
            online_plan, subscriber_user, organization, stripe_subscription_id="sub_to_uncancel"
        )

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 200, response.content
        mock_modify.assert_called_once()
        assert mock_modify.call_args.args[0] == "sub_to_uncancel"
        assert mock_modify.call_args.kwargs["cancel_at_period_end"] is False
        assert mock_modify.call_args.kwargs["stripe_account"] == "acct_test_org"
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is False

    @mock.patch("events.service.subscription_uncancel.stripe.Subscription.modify")
    def test_stripe_failure_leaves_the_flag_set(
        self,
        mock_modify: mock.Mock,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        """A refused Stripe modify must not record a renewal Stripe never accepted."""
        mock_modify.side_effect = stripe.error.APIConnectionError("boom")
        subscription = _scheduled_cancel(online_plan, subscriber_user, organization, stripe_subscription_id="sub_flaky")

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 502, response.content
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is True

    @mock.patch("events.service.subscription_uncancel.stripe.Subscription.modify")
    def test_online_without_stripe_link_stays_local(
        self,
        mock_modify: mock.Mock,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        online_plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        """An ONLINE row cancelled before its first checkout has no Stripe record to update."""
        subscription = _scheduled_cancel(online_plan, subscriber_user, organization)

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 200, response.content
        mock_modify.assert_not_called()
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is False

    def test_not_scheduled_is_a_no_op(
        self,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = subscription_service.create_subscription(plan, subscriber_user)

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 200, response.content
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is False

    def test_archived_plan_is_refused(
        self,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)
        subscription_service.archive_plan(plan)

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 400, response.content
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is True

    def test_terminal_subscription_is_invisible_to_the_member(
        self,
        subscriber_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        """A cancelled row is excluded by the member's non-terminal lookup — 404, not 400."""
        subscription = _scheduled_cancel(plan, subscriber_user, organization)
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.save(update_fields=["status"])

        response = subscriber_client.post(_member_url(organization))

        assert response.status_code == 404, response.content

    def test_member_cannot_touch_someone_elses_subscription(
        self,
        nonmember_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)

        response = nonmember_client.post(_member_url(organization))

        assert response.status_code == 404, response.content
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is True


class TestStaffUncancel:
    def test_staff_clears_a_members_scheduled_cancellation(
        self,
        organization_owner_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)

        response = organization_owner_client.post(_admin_url(organization, subscription))

        assert response.status_code == 200, response.content
        assert response.json()["cancel_at_period_end"] is False
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is False

    def test_terminal_subscription_is_refused(
        self,
        organization_owner_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)
        subscription.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        subscription.save(update_fields=["status"])

        response = organization_owner_client.post(_admin_url(organization, subscription))

        assert response.status_code == 400, response.content

    def test_non_staff_is_refused(
        self,
        member_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)

        response = member_client.post(_admin_url(organization, subscription))

        assert response.status_code == 403, response.content
        subscription.refresh_from_db()
        assert subscription.cancel_at_period_end is True

    def test_other_organization_subscription_is_not_found(
        self,
        organization_owner_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        other_org = Organization.objects.create(name="Other", slug="other", owner=organization_owner_user)
        subscription = _scheduled_cancel(plan, subscriber_user, organization)

        url = reverse("api:uncancel_subscription", kwargs={"slug": other_org.slug, "sub_id": subscription.id})
        response = organization_owner_client.post(url)

        assert response.status_code == 404, response.content


class TestStuckStateResolved:
    def test_pause_works_again_after_uncancel(
        self,
        organization_owner_client: Client,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        """The exact deadlock from #808: a scheduled cancel could be neither paused nor undone."""
        # The initial payment gives the row a period boundary — without one the
        # scheduled cancel is upgraded to an immediate one (see
        # ``cancel_subscription``) and there is nothing left to undo.
        subscription = subscription_service.create_subscription(
            plan,
            subscriber_user,
            initial_payment=subscription_service.InitialPayment(
                amount=plan.price,
                currency=plan.currency,
                recorded_by=organization.owner,
            ),
        )
        subscription_service.cancel_subscription(subscription, immediate=False)

        pause_url = reverse("api:pause_subscription", kwargs={"slug": organization.slug, "sub_id": subscription.id})
        assert organization_owner_client.post(pause_url).status_code == 400

        assert organization_owner_client.post(_admin_url(organization, subscription)).status_code == 200

        assert organization_owner_client.post(pause_url).status_code == 200
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED
        assert subscription.cancel_at_period_end is False


class TestServiceGuards:
    def test_terminal_row_raises_400(
        self,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.save(update_fields=["status"])

        with pytest.raises(HttpError) as exc_info:
            subscription_uncancel.uncancel_subscription(subscription)

        assert exc_info.value.status_code == 400

    def test_archived_plan_raises_400(
        self,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        subscription = _scheduled_cancel(plan, subscriber_user, organization)
        subscription_service.archive_plan(plan)

        with pytest.raises(HttpError) as exc_info:
            subscription_uncancel.uncancel_subscription(subscription)

        assert exc_info.value.status_code == 400

    def test_archived_plan_does_not_block_an_unscheduled_row(
        self,
        subscriber_user: RevelUser,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
    ) -> None:
        """Nothing to undo means nothing to refuse — the row is already in the requested state."""
        subscription = subscription_service.create_subscription(plan, subscriber_user)
        subscription_service.archive_plan(plan)

        result = subscription_uncancel.uncancel_subscription(subscription)

        assert result.cancel_at_period_end is False
