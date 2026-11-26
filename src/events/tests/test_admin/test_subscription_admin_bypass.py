"""The MembershipSubscription admin cannot add or revert rows past the service layer (#1436)."""

import typing as t
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
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
