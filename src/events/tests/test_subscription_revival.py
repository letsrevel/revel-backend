"""Tests for the EXPIRED → ACTIVE revival flow (OFFLINE and ONLINE branches)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    CustomerProfile,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_service
from events.service.subscription_service import InitialPayment


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="rev_user", email="rev_user@example.com", password="pass")


@pytest.fixture
def staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="rev_staff", email="rev_staff@example.com", password="pass")


@pytest.fixture
def expired_sub(
    plan: MembershipSubscriptionPlan,
    organization: Organization,
    subscriber: RevelUser,
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.EXPIRED,
        expired_at=timezone.now() - timedelta(days=5),
    )


@pytest.fixture
def payload(plan: MembershipSubscriptionPlan, staff_user: RevelUser) -> InitialPayment:
    return InitialPayment(
        amount=plan.price,
        currency=plan.currency,
        recorded_by=staff_user,
    )


@pytest.mark.django_db
class TestRevivalRefusals:
    def test_non_expired_subscription(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        payload: InitialPayment,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(sub, initial_payment=payload)
        assert ei.value.status_code == 400

    def test_outside_window(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        payload: InitialPayment,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=60),
        )
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(sub, initial_payment=payload)
        assert ei.value.status_code == 400

    def test_revival_disabled_for_org(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        payload: InitialPayment,
    ) -> None:
        organization.membership_subscription_revival_window_days = 0
        organization.save(update_fields=["membership_subscription_revival_window_days"])
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(expired_sub, initial_payment=payload)
        assert ei.value.status_code == 400

    def test_legacy_no_expired_at(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        payload: InitialPayment,
    ) -> None:
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=None,
        )
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(sub, initial_payment=payload)
        assert ei.value.status_code == 400

    def test_user_has_another_active_sub(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        payload: InitialPayment,
    ) -> None:
        # Create another non-terminal sub for the same user/org
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        expired = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(expired, initial_payment=payload)
        assert ei.value.status_code == 400

    def test_banned_user_refused(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        payload: InitialPayment,
    ) -> None:
        OrganizationMember.objects.create(
            user=subscriber,
            organization=organization,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(expired_sub, initial_payment=payload)
        assert ei.value.status_code == 403

    def test_offline_requires_initial_payment(self, expired_sub: MembershipSubscription) -> None:
        with pytest.raises(HttpError) as ei:
            subscription_service.revive_subscription(expired_sub, initial_payment=None)
        assert ei.value.status_code == 400


@pytest.mark.django_db
class TestOfflineRevivalSuccess:
    def test_offline_revival_reactivates_and_records_payment(
        self,
        expired_sub: MembershipSubscription,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
        staff_user: RevelUser,
    ) -> None:
        result, client_secret = subscription_service.revive_subscription(
            expired_sub, initial_payment=payload, revived_by=staff_user
        )
        result.refresh_from_db()
        assert client_secret is None
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert result.current_period_start is not None
        assert result.current_period_end is not None
        # expired_at preserved as audit trail
        assert result.expired_at is not None
        # A payment was recorded
        assert result.payments.count() == 1
        payment = result.payments.first()
        assert payment is not None
        assert payment.amount == plan.price

    def test_offline_revival_reactivates_organization_member(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
    ) -> None:
        # Pre-create an OrganizationMember in CANCELLED state (as set when sub expired).
        # The signal only updates *existing* members; creation is handled by the service.
        OrganizationMember.objects.create(
            user=subscriber,
            organization=organization,
            tier=plan.tier,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        subscription_service.revive_subscription(expired_sub, initial_payment=payload)
        member = OrganizationMember.objects.get(user=subscriber, organization=organization)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_offline_revival_does_not_fire_renewal_succeeded(
        self,
        expired_sub: MembershipSubscription,
        payload: InitialPayment,
    ) -> None:
        from notifications.enums import NotificationType
        from notifications.models import Notification

        subscription_service.revive_subscription(expired_sub, initial_payment=payload)
        # The revival success itself is the user-visible confirmation; we
        # explicitly suppress RENEWAL_SUCCEEDED.
        assert not Notification.objects.filter(
            user=expired_sub.user,
            notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        ).exists()


@pytest.mark.django_db
class TestOnlineRevivalSuccess:
    def _make_stripe_connected(self, org: Organization) -> None:
        org.stripe_account_id = "acct_test_xyz"
        org.stripe_charges_enabled = True
        org.stripe_details_submitted = True
        org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])

    def test_online_revival_creates_new_stripe_subscription(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_revival_x",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthly",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_revival_x",
            stripe_product_id="prod_revival_x",
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
            stripe_subscription_id="sub_old_dead",
        )

        with (
            patch("events.service.subscription_stripe_service.stripe.Subscription.create") as create_mock,
            patch("events.service.subscription_stripe_service.stripe.Subscription.cancel") as cancel_mock,
        ):
            create_mock.return_value = MagicMock(
                id="sub_new_alive",
                latest_invoice={"payment_intent": {"client_secret": "pi_revival_secret"}},
            )
            result, client_secret = subscription_service.revive_subscription(sub)

        # C2: the old (possibly still-dunning) Stripe sub is closed before its
        # id is overwritten, so a late retry success can't double-bill.
        cancel_mock.assert_called_once()
        assert cancel_mock.call_args.args[0] == "sub_old_dead"

        result.refresh_from_db()
        assert result.stripe_subscription_id == "sub_new_alive"
        assert result.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert result.current_period_start is None
        assert result.current_period_end is None
        assert client_secret == "pi_revival_secret"

        # Verify Stripe was called with the right parameters.
        create_mock.assert_called_once()
        call_kwargs = create_mock.call_args.kwargs
        assert call_kwargs["customer"] == "cus_revival_x"
        assert call_kwargs["items"] == [{"price": "price_revival_x"}]
        assert call_kwargs["payment_behavior"] == "default_incomplete"
        assert call_kwargs["stripe_account"] == "acct_test_xyz"

    def test_online_revival_stripe_failure_raises_502(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_revival_fail",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthly2",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_revival_y",
            stripe_product_id="prod_revival_y",
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
            stripe_subscription_id="sub_old_dead2",
        )

        import stripe as stripe_lib

        with (
            patch("events.service.subscription_stripe_service.stripe.Subscription.create") as create_mock,
            patch("events.service.subscription_stripe_service.stripe.Subscription.cancel"),
        ):
            create_mock.side_effect = stripe_lib.error.APIConnectionError("network failure")
            with pytest.raises(HttpError) as exc:
                subscription_service.revive_subscription(sub)

        assert exc.value.status_code == 502
        # Local row must NOT have been mutated — the subscription stays EXPIRED.
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.stripe_subscription_id == "sub_old_dead2"

    def test_online_revival_passes_metadata_and_idempotency_key(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_meta_x",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthlyMeta",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_meta_x",
            stripe_product_id="prod_meta_x",
        )
        expired_at = timezone.now() - timedelta(days=1)
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=expired_at,
        )

        with (
            patch("events.service.subscription_stripe_service.stripe.Subscription.create") as create_mock,
            patch("events.service.subscription_stripe_service.stripe.Subscription.cancel"),
        ):
            create_mock.return_value = MagicMock(
                id="sub_meta_new",
                latest_invoice={"payment_intent": {"client_secret": "pi_meta_secret"}},
            )
            subscription_service.revive_subscription(sub)

        create_kwargs = create_mock.call_args.kwargs
        assert "idempotency_key" in create_kwargs
        assert "sub-revival" in create_kwargs["idempotency_key"]
        assert str(sub.pk) in create_kwargs["idempotency_key"]
        assert "metadata" in create_kwargs
        meta = create_kwargs["metadata"]
        assert meta["revel_subscription_id"] == str(sub.pk)
        assert meta["revel_user_id"] == str(subscriber.pk)
        assert meta["revel_org_id"] == str(organization.pk)
        assert meta["revel_plan_id"] == str(online_plan.pk)

    def test_online_revival_missing_client_secret_triggers_cleanup_and_502(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_cleanup_x",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthlyCleanup",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_cleanup_x",
            stripe_product_id="prod_cleanup_x",
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        stripe_sub_mock = MagicMock()
        stripe_sub_mock.id = "sub_dangling"
        stripe_sub_mock.latest_invoice = None

        with (
            patch("events.service.subscription_stripe_service.stripe.Subscription.create") as create_mock,
            patch("events.service.subscription_stripe_service.stripe.Subscription.cancel") as cancel_mock,
        ):
            create_mock.return_value = stripe_sub_mock

            with pytest.raises(HttpError) as ei:
                subscription_service.revive_subscription(sub)
            assert ei.value.status_code == 502

        # Cleanup was attempted on the dangling Stripe sub.
        cancel_mock.assert_called_once()

        # Local row must remain EXPIRED and untouched.
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.stripe_subscription_id is None


# ---------------------------------------------------------------------------
# Controller endpoint tests
# ---------------------------------------------------------------------------


def _auth_client(user: RevelUser) -> Client:
    """Return a Django test client authenticated as *user* via JWT Bearer token."""
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.mark.django_db
class TestSelfReviveEndpoint:
    """POST /api/me/organizations/{org_id}/subscription/revive"""

    def test_offline_self_revive_succeeds(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """OFFLINE revival returns 200 with null client_secret and active subscription."""
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        client = _auth_client(subscriber)
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        resp = client.post(
            url,
            data={"amount": str(plan.price), "currency": plan.currency},
            content_type="application/json",
        )

        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["client_secret"] is None
        assert body["subscription"]["status"] == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_no_expired_sub_returns_404(
        self,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """No EXPIRED subscription in org → 404."""
        client = _auth_client(subscriber)
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        resp = client.post(url, data={}, content_type="application/json")

        assert resp.status_code == 404

    def test_unauthenticated_returns_401(
        self,
        organization: Organization,
    ) -> None:
        """No JWT → 401."""
        client = Client()
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        resp = client.post(url, data={}, content_type="application/json")

        assert resp.status_code == 401

    def test_picks_most_recent_expired_sub(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """When multiple EXPIRED subs exist, the most recently expired one is revived."""
        older = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=10),
        )
        newer = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        client = _auth_client(subscriber)
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        resp = client.post(
            url,
            data={"amount": str(plan.price), "currency": plan.currency},
            content_type="application/json",
        )

        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["subscription"]["id"] == str(newer.pk)
        # The older sub must remain EXPIRED.
        older.refresh_from_db()
        assert older.status == MembershipSubscription.SubscriptionStatus.EXPIRED


@pytest.mark.django_db
class TestStaffReviveEndpoint:
    """POST /api/organization-admin/{slug}/subscriptions/{sub_id}/revive"""

    def test_owner_can_revive_member(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """Org owner with manage_subscriptions permission can revive an EXPIRED sub."""
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        client = _auth_client(organization_owner_user)
        url = f"/api/organization-admin/{organization.slug}/subscriptions/{sub.pk}/revive"
        resp = client.post(
            url,
            data={"amount": str(plan.price), "currency": plan.currency},
            content_type="application/json",
        )

        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["subscription"]["status"] == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert body["client_secret"] is None
        # Staff response includes user PII.
        assert body["subscription"]["user_id"] == str(subscriber.pk)

    def test_non_org_user_gets_404(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A user with no org relationship cannot access the staff endpoint (org is 404)."""
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        client = _auth_client(subscriber)
        url = f"/api/organization-admin/{organization.slug}/subscriptions/{sub.pk}/revive"
        resp = client.post(
            url,
            data={"amount": str(plan.price), "currency": plan.currency},
            content_type="application/json",
        )

        assert resp.status_code == 404

    def test_wrong_org_sub_returns_404(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """Sub belonging to a different org → 404 (scoped lookup)."""
        other_owner = django_user_model.objects.create_user(
            username="other_owner2", email="other2@example.com", password="pass"
        )
        other_org = Organization.objects.create(name="Other Org", slug="other-org2", owner=other_owner)
        other_tier = MembershipTier.objects.create(organization=other_org, name="OtherPro")
        other_plan = MembershipSubscriptionPlan.objects.create(
            tier=other_tier,
            name="Monthly",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=other_plan,
            organization=other_org,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        client = _auth_client(organization_owner_user)
        url = f"/api/organization-admin/{organization.slug}/subscriptions/{sub.pk}/revive"
        resp = client.post(
            url,
            data={"amount": str(other_plan.price), "currency": other_plan.currency},
            content_type="application/json",
        )

        assert resp.status_code == 404
