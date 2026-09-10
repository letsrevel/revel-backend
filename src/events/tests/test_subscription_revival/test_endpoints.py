"""Controller-endpoint tests for the EXPIRED → ACTIVE revival flow."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.tests.test_subscription_revival.conftest import _apply_revival_refusal

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

    def test_offline_self_revive_is_refused(
        self,
        plan: MembershipSubscriptionPlan,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """OFFLINE plans cannot be revived self-service: members must never self-record money.

        A member-supplied amount would let an expired offline subscriber grant
        themselves an ACTIVE period for free (e.g. amount=0) with a self-authored
        ledger entry. Staff revive offline subscriptions via the admin endpoint.
        """
        sub = MembershipSubscription.objects.create(
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
            data={"amount": "0", "currency": plan.currency},
            content_type="application/json",
        )

        assert resp.status_code == 400, resp.content
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert not sub.payments.exists()

    def test_no_expired_sub_returns_404(
        self,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """No EXPIRED subscription in org → 404 with a translated, neutral detail."""
        client = _auth_client(subscriber)
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        resp = client.post(url, data={}, content_type="application/json")

        assert resp.status_code == 404
        # Not ninja's untranslated ``"Not Found"``, which the frontend prints verbatim.
        assert resp.json() == {"detail": "Not found."}

    @pytest.mark.parametrize("refusal", ["banned", "blacklisted"])
    def test_member_revive_refusal_is_403_with_neutral_copy(
        self,
        refusal: str,
        plan: MembershipSubscriptionPlan,
        expired_sub: MembershipSubscription,
        organization: Organization,
        subscriber: RevelUser,
        staff_user: RevelUser,
    ) -> None:
        """The endpoint really can answer 403 (now declared) and says the same thing either way."""
        plan.payment_method = MembershipSubscriptionPlan.PaymentMethod.ONLINE
        plan.save(update_fields=["payment_method"])
        _apply_revival_refusal(refusal, organization, subscriber, staff_user)

        client = _auth_client(subscriber)
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        resp = client.post(url, data={}, content_type="application/json")

        assert resp.status_code == 403, resp.content
        assert resp.json() == {"detail": "You can't rejoin this organization."}

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
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
    ) -> None:
        """When multiple EXPIRED subs exist, the most recently expired one is revived."""
        online_plan = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly online",
            price=Decimal("10"),
            currency="EUR",
            period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        )
        older = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=10),
        )
        newer = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=organization,
            status=MembershipSubscription.SubscriptionStatus.EXPIRED,
            expired_at=timezone.now() - timedelta(days=1),
        )

        client = _auth_client(subscriber)
        url = f"/api/me/organizations/{organization.pk}/subscription/revive"
        with patch("events.controllers.me_subscriptions.subscription_service.revive_subscription") as mock_revive:
            mock_revive.return_value = (newer, "cs_secret")
            resp = client.post(url, data={}, content_type="application/json")

        assert resp.status_code == 200, resp.content
        # The endpoint must resolve the most recently expired sub and never
        # forward a member-supplied initial payment.
        (called_sub,), called_kwargs = mock_revive.call_args
        assert called_sub.pk == newer.pk
        assert called_kwargs["initial_payment"] is None
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
        assert body["checkout_url"] is None
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
