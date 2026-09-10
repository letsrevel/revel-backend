"""Service-layer tests for the EXPIRED → ACTIVE revival flow (OFFLINE and ONLINE branches)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    CustomerProfile,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription.lifecycle import InitialPayment
from events.service.subscription.stripe.checkout import clear_stale_pending_checkout
from events.tests.test_subscription_revival.conftest import _apply_revival_refusal


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
            subscription_lifecycle.revive_subscription(sub, initial_payment=payload)
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
            subscription_lifecycle.revive_subscription(sub, initial_payment=payload)
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
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload)
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
            subscription_lifecycle.revive_subscription(sub, initial_payment=payload)
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
            subscription_lifecycle.revive_subscription(expired, initial_payment=payload)
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
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload)
        assert ei.value.status_code == 403

    def test_hard_blacklisted_user_refused(
        self,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        payload: InitialPayment,
    ) -> None:
        """A hard-blacklisted user cannot revive, mirroring the create_subscription guard."""
        Blacklist.objects.create(organization=organization, user=subscriber, created_by=staff_user)
        with pytest.raises(HttpError) as ei:
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload)
        assert ei.value.status_code == 403

    @pytest.mark.parametrize("refusal", ["banned", "blacklisted"])
    def test_staff_caller_keeps_the_specific_reason(
        self,
        refusal: str,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        payload: InitialPayment,
    ) -> None:
        """The admin console is where "banned" vs "blacklisted" is actionable — keep the detail."""
        _apply_revival_refusal(refusal, organization, subscriber, staff_user)
        with pytest.raises(HttpError) as ei:
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload, revived_by=staff_user)
        assert ei.value.status_code == 403
        assert refusal in str(ei.value)

    @pytest.mark.parametrize("refusal", ["banned", "blacklisted"])
    def test_member_caller_gets_one_neutral_non_disclosing_refusal(
        self,
        refusal: str,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
        payload: InitialPayment,
    ) -> None:
        """The member's own page must not confirm the blacklist, nor speak in the third person."""
        _apply_revival_refusal(refusal, organization, subscriber, staff_user)
        with pytest.raises(HttpError) as ei:
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload, revived_by=subscriber)
        assert ei.value.status_code == 403
        message = str(ei.value)
        assert message == "You can't rejoin this organization."
        assert "blacklist" not in message.lower()
        assert "banned" not in message.lower()

    def test_member_caller_duplicate_active_speaks_first_person(
        self,
        expired_sub: MembershipSubscription,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
        payload: InitialPayment,
    ) -> None:
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )
        with pytest.raises(HttpError) as ei:
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload, revived_by=subscriber)
        assert ei.value.status_code == 400
        assert str(ei.value) == "You already have an active subscription in this organization."

    def test_offline_requires_initial_payment(self, expired_sub: MembershipSubscription) -> None:
        with pytest.raises(HttpError) as ei:
            subscription_lifecycle.revive_subscription(expired_sub, initial_payment=None)
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
        result, client_secret = subscription_lifecycle.revive_subscription(
            expired_sub, initial_payment=payload, revived_by=staff_user
        )
        result.refresh_from_db()
        assert client_secret is None
        assert result.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert result.current_period_start is not None
        assert result.current_period_end is not None
        # expired_at is consumed by the revival — a future lapse must stamp a
        # fresh one (the old value stays in simple-history for audit).
        assert result.expired_at is None
        # A payment was recorded
        assert result.payments.count() == 1
        payment = result.payments.first()
        assert payment is not None
        assert payment.amount == plan.price

    def test_second_lapse_opens_fresh_revival_window(
        self,
        expired_sub: MembershipSubscription,
        plan: MembershipSubscriptionPlan,
        payload: InitialPayment,
        staff_user: RevelUser,
    ) -> None:
        """Revival is not one-shot: a second lapse anchors on a FRESH expired_at.

        Regression: ``expired_at`` used to survive the revival (all writers are
        ``expired_at or now``-guarded), so the second lapse kept the stale
        first-expiry timestamp and ``_validate_revivable`` wrongly refused with
        "revival window elapsed".
        """
        assert expired_sub.expired_at is not None
        subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload, revived_by=staff_user)
        # Re-fetch into a fresh instance (mypy can't see refresh_from_db's mutation).
        expired_sub = MembershipSubscription.objects.get(pk=expired_sub.pk)
        assert expired_sub.expired_at is None

        # Second lapse: mimic the grace-expiry sweep's fresh stamping (its
        # ``expired_at or now`` guard finds None now, so it stamps anew).
        expired_sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
        expired_sub.expired_at = timezone.now()
        expired_sub.save(update_fields=["status", "expired_at"])

        revived, checkout_url = subscription_lifecycle.revive_subscription(
            expired_sub, initial_payment=payload, revived_by=staff_user
        )
        revived.refresh_from_db()
        assert checkout_url is None
        assert revived.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert revived.expired_at is None

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
        subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload)
        member = OrganizationMember.objects.get(user=subscriber, organization=organization)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_offline_revival_does_not_fire_renewal_succeeded(
        self,
        expired_sub: MembershipSubscription,
        payload: InitialPayment,
    ) -> None:
        from notifications.enums import NotificationType
        from notifications.models import Notification

        subscription_lifecycle.revive_subscription(expired_sub, initial_payment=payload)
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

    def test_online_revival_mints_checkout_session(
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
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel") as cancel_mock,
        ):
            create_mock.return_value = MagicMock(id="cs_revival", url="https://checkout.stripe.com/c/pay/cs_revival")
            result, checkout_url = subscription_lifecycle.revive_subscription(sub)

        # C2: the old (possibly still-dunning) Stripe sub is closed before its
        # id is cleared, so a late retry success can't double-bill.
        cancel_mock.assert_called_once()
        assert cancel_mock.call_args.args[0] == "sub_old_dead"

        result.refresh_from_db()
        # The fresh Stripe Subscription only exists once the session completes;
        # the old dead id must be cleared so sync/reconcile can't re-observe it.
        assert result.stripe_subscription_id is None
        assert result.stripe_checkout_session_id == "cs_revival"
        assert result.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert result.current_period_start is None
        assert result.current_period_end is None
        assert checkout_url == "https://checkout.stripe.com/c/pay/cs_revival"

        # Verify Stripe was called with the right parameters.
        create_mock.assert_called_once()
        call_kwargs = create_mock.call_args.kwargs
        assert call_kwargs["mode"] == "subscription"
        assert call_kwargs["customer"] == "cus_revival_x"
        assert call_kwargs["line_items"] == [{"price": "price_revival_x", "quantity": 1}]
        assert call_kwargs["stripe_account"] == "acct_test_xyz"

    def test_online_member_revival_sends_no_checkout_notification(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """Member-initiated revival redirects the member — no checkout email."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_member_rev",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthlySelf",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_self_x",
            stripe_product_id="prod_self_x",
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        with (
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
        ):
            create_mock.return_value = MagicMock(id="cs_self", url="https://checkout.stripe.com/c/pay/cs_self")
            subscription_lifecycle.revive_subscription(sub, revived_by=subscriber)

        assert not Notification.objects.filter(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT,
        ).exists()

    def test_online_staff_revival_notifies_member_with_checkout_link(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
    ) -> None:
        """Staff cannot pay on the member's behalf — the member gets the link."""
        from notifications.enums import NotificationType
        from notifications.models import Notification

        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_staff_rev",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthlyStaff",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_staff_x",
            stripe_product_id="prod_staff_x",
        )
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        with (
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
        ):
            create_mock.return_value = MagicMock(id="cs_staff", url="https://checkout.stripe.com/c/pay/cs_staff")
            _, checkout_url = subscription_lifecycle.revive_subscription(
                sub,
                revived_by=staff_user,
                enforce_sales_status=False,
            )

        assert checkout_url == "https://checkout.stripe.com/c/pay/cs_staff"
        notification = Notification.objects.get(
            user=subscriber,
            notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT,
        )
        assert notification.context["checkout_url"] == checkout_url
        assert notification.context["organization_name"] == organization.name
        assert notification.context["plan_name"] == online_plan.name

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
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
        ):
            create_mock.side_effect = stripe_lib.error.APIConnectionError("network failure")
            with pytest.raises(HttpError) as exc:
                subscription_lifecycle.revive_subscription(sub)

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
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
        ):
            create_mock.return_value = MagicMock(id="cs_meta", url="https://checkout.stripe.com/c/pay/cs_meta")
            subscription_lifecycle.revive_subscription(sub)

        create_kwargs = create_mock.call_args.kwargs
        assert "idempotency_key" in create_kwargs
        assert "sub-revival" in create_kwargs["idempotency_key"]
        assert str(sub.pk) in create_kwargs["idempotency_key"]
        assert create_kwargs["metadata"] == {"membership_subscription_id": str(sub.pk)}
        assert create_kwargs["subscription_data"]["metadata"] == {"membership_subscription_id": str(sub.pk)}

    def test_second_revival_after_revert_uses_a_fresh_idempotency_key(
        self,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """A re-revive must not replay the first attempt's Stripe idempotency key.

        An abandoned revival is reverted to EXPIRED with ``expired_at``
        deliberately preserved, while the session-create payload carries a
        freshly computed ``expires_at``. A key derived from row state would
        therefore repeat with different params — which Stripe rejects with an
        IdempotencyError for ~24h, 502-ing every retry (#803).
        """
        self._make_stripe_connected(organization)
        CustomerProfile.objects.create(
            user=subscriber,
            organization=organization,
            stripe_customer_id="cus_rekey_x",
        )
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="OnlineMonthlyRekey",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
            stripe_price_id="price_rekey_x",
            stripe_product_id="prod_rekey_x",
        )
        expired_at = timezone.now() - timedelta(days=1)
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=expired_at,
        )
        # A payment-bearing row is what makes the revert (rather than a delete)
        # happen — the ledger cascades on delete.
        MembershipPayment.objects.create(
            subscription=sub,
            amount=online_plan.price,
            currency=online_plan.currency,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=31),
            period_end=expired_at,
        )

        with (
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
        ):
            create_mock.return_value = MagicMock(id="cs_rekey_1", url="https://checkout.stripe.com/c/pay/cs_rekey_1")
            subscription_lifecycle.revive_subscription(sub)
            first_key = create_mock.call_args.kwargs["idempotency_key"]

            # Member abandons the checkout: the row is reverted to EXPIRED with
            # expired_at intact (#802), then they click Rejoin again.
            sub.refresh_from_db()
            clear_stale_pending_checkout(sub)
            sub.refresh_from_db()
            assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
            assert sub.expired_at == expired_at

            create_mock.return_value = MagicMock(id="cs_rekey_2", url="https://checkout.stripe.com/c/pay/cs_rekey_2")
            subscription_lifecycle.revive_subscription(sub)
            second_key = create_mock.call_args.kwargs["idempotency_key"]

        assert first_key != second_key

    def test_online_revival_missing_session_url_raises_502(
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

        session_mock = MagicMock()
        session_mock.id = "cs_dangling"
        session_mock.url = None

        with (
            patch("events.service.subscription.stripe.checkout.stripe.checkout.Session.create") as create_mock,
            patch("events.service.subscription.stripe.checkout.stripe.Subscription.cancel"),
        ):
            create_mock.return_value = session_mock

            with pytest.raises(HttpError) as ei:
                subscription_lifecycle.revive_subscription(sub)
            assert ei.value.status_code == 502

        # Local row must remain EXPIRED and untouched.
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert sub.stripe_subscription_id is None
        assert sub.stripe_checkout_session_id == ""
