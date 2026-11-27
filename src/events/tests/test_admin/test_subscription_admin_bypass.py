"""The MembershipSubscription admin cannot add or revert rows past the service layer (#1436)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    CustomerProfile,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings: t.Any) -> None:
    """Admin pages render static URLs; the hashed manifest storage has no manifest in tests."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(superuser: RevelUser) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def plans(organization: Organization) -> tuple[MembershipSubscriptionPlan, MembershipSubscriptionPlan]:
    tier = MembershipTier.objects.create(organization=organization, name="Pro")
    kwargs: dict[str, t.Any] = {
        "tier": tier,
        "currency": "EUR",
        "period_unit": MembershipSubscriptionPlan.PeriodUnit.MONTH,
        "period_count": 1,
    }
    a = MembershipSubscriptionPlan.objects.create(name="A", price=Decimal("10.00"), **kwargs)
    b = MembershipSubscriptionPlan.objects.create(name="B", price=Decimal("20.00"), **kwargs)
    return a, b


@pytest.fixture
def subscription(
    organization: Organization,
    member_user: RevelUser,
    plans: tuple[MembershipSubscriptionPlan, MembershipSubscriptionPlan],
) -> MembershipSubscription:
    sub = MembershipSubscription.objects.create(
        user=member_user,
        plan=plans[0],
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    # A plan change after creation, as change_plan does, so there is an older history entry to revert to.
    sub.plan = plans[1]
    sub.save()
    return sub


class TestSubscriptionAdminBypass:
    def test_add_view_is_refused(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:events_membershipsubscription_add"))
        assert response.status_code == 403

    def test_history_revert_is_refused_and_changes_nothing(
        self,
        admin_client: Client,
        subscription: MembershipSubscription,
        plans: tuple[MembershipSubscriptionPlan, MembershipSubscriptionPlan],
    ) -> None:
        old = subscription.history.order_by("history_date").first()
        assert old is not None
        assert old.plan_id == plans[0].pk
        url = reverse("admin:events_membershipsubscription_simple_history", args=[subscription.pk, old.history_id])

        assert admin_client.post(url, {"cancel_at_period_end": "on"}).status_code == 403

        subscription.refresh_from_db()
        assert subscription.plan_id == plans[1].pk
        assert subscription.cancel_at_period_end is False

    def test_history_pages_stay_viewable(self, admin_client: Client, subscription: MembershipSubscription) -> None:
        """View-only history still opens the list and the old version."""
        old = subscription.history.order_by("history_date").first()
        assert old is not None

        history_url = reverse("admin:events_membershipsubscription_history", args=[subscription.pk])
        form_url = reverse("admin:events_membershipsubscription_simple_history", args=[subscription.pk, old.history_id])

        assert admin_client.get(history_url).status_code == 200
        assert admin_client.get(form_url).status_code == 200

    def test_change_post_does_not_alter_locked_fields(
        self, admin_client: Client, subscription: MembershipSubscription, django_user_model: t.Any
    ) -> None:
        other = django_user_model.objects.create_user(username="other", email="other@example.com", password="x")
        original_user_id = subscription.user_id
        url = reverse("admin:events_membershipsubscription_change", args=[subscription.pk])

        admin_client.post(url, {"user": str(other.pk), "cancel_at_period_end": "on", "held_member": "True"})

        subscription.refresh_from_db()
        assert subscription.user_id == original_user_id
        assert subscription.cancel_at_period_end is False
        assert subscription.held_member is None

    def test_delete_is_refused_for_subscription(
        self, admin_client: Client, subscription: MembershipSubscription
    ) -> None:
        url = reverse("admin:events_membershipsubscription_delete", args=[subscription.pk])
        assert admin_client.get(url).status_code == 403
        assert admin_client.post(url, {"post": "yes"}).status_code == 403
        assert MembershipSubscription.objects.filter(pk=subscription.pk).exists()

    def test_bulk_delete_is_refused_for_subscription(
        self, admin_client: Client, subscription: MembershipSubscription
    ) -> None:
        url = reverse("admin:events_membershipsubscription_changelist")
        data = {"action": "delete_selected", "_selected_action": [str(subscription.pk)], "post": "yes"}
        admin_client.post(url, data)
        assert MembershipSubscription.objects.filter(pk=subscription.pk).exists()


class TestSiblingAdmins:
    @pytest.fixture
    def payment(self, subscription: MembershipSubscription) -> MembershipPayment:
        now = timezone.now()
        return MembershipPayment.objects.create(
            subscription=subscription,
            amount=Decimal("20.00"),
            currency="EUR",
            period_start=now,
            period_end=now + timedelta(days=30),
        )

    def test_plan_history_revert_is_refused(
        self, admin_client: Client, plans: tuple[MembershipSubscriptionPlan, MembershipSubscriptionPlan]
    ) -> None:
        plan = plans[0]
        plan.stripe_price_id = "price_new"
        plan.save()
        old = plan.history.order_by("history_date").first()
        assert old is not None
        url = reverse("admin:events_membershipsubscriptionplan_simple_history", args=[plan.pk, old.history_id])

        assert admin_client.post(url, {"name": "A"}).status_code == 403

        plan.refresh_from_db()
        assert plan.stripe_price_id == "price_new"

    def test_customer_profile_history_revert_is_refused(
        self, admin_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        profile = CustomerProfile.objects.create(
            user=member_user, organization=organization, stripe_customer_id="cus_1"
        )
        profile.stripe_customer_id = "cus_2"
        profile.save()
        old = profile.history.order_by("history_date").first()
        assert old is not None
        url = reverse("admin:events_customerprofile_simple_history", args=[profile.pk, old.history_id])

        assert admin_client.post(url, {"user": str(member_user.pk)}).status_code == 403

        profile.refresh_from_db()
        assert profile.stripe_customer_id == "cus_2"

    def test_payment_history_revert_is_refused(self, admin_client: Client, payment: MembershipPayment) -> None:
        url = reverse("admin:events_membershippayment_simple_history", args=[payment.pk, 1])
        assert admin_client.post(url, {"amount": "1.00"}).status_code == 403
        payment.refresh_from_db()
        assert payment.amount == Decimal("20.00")

    def test_payment_delete_is_refused(self, admin_client: Client, payment: MembershipPayment) -> None:
        url = reverse("admin:events_membershippayment_delete", args=[payment.pk])
        assert admin_client.get(url).status_code == 403
        assert admin_client.post(url, {"post": "yes"}).status_code == 403
        assert MembershipPayment.objects.filter(pk=payment.pk).exists()

    def test_bulk_delete_is_refused_for_payment(self, admin_client: Client, payment: MembershipPayment) -> None:
        url = reverse("admin:events_membershippayment_changelist")
        data = {"action": "delete_selected", "_selected_action": [str(payment.pk)], "post": "yes"}
        admin_client.post(url, data)
        assert MembershipPayment.objects.filter(pk=payment.pk).exists()
