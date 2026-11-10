"""Tests for editing/voiding manual membership payments and backdated first payments."""

import datetime
import typing as t
from decimal import Decimal
from unittest import mock

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import revenue_aggregation
from events.service.subscription import lifecycle as subscription_lifecycle
from events.service.subscription import plans as subscription_plans
from notifications.models import Notification

pytestmark = pytest.mark.django_db

Status = MembershipPayment.PaymentStatus


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.get(organization=organization, name="General membership")


@pytest.fixture
def plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    return subscription_plans.create_plan(
        tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="edit_sub", email="edit-sub@example.com", password="pass")


@pytest.fixture
def sub(
    plan: MembershipSubscriptionPlan, subscriber: RevelUser, organization_owner_user: RevelUser
) -> MembershipSubscription:
    """Subscription with one SUCCEEDED payment recorded 10 days ago (period ends ~20 days from now)."""
    return subscription_lifecycle.create_subscription(
        plan,
        subscriber,
        initial_payment=subscription_lifecycle.InitialPayment(
            amount=Decimal("10.00"),
            currency="EUR",
            recorded_by=organization_owner_user,
            occurred_at=timezone.now() - datetime.timedelta(days=10),
        ),
    )


def _record(sub: MembershipSubscription, owner: RevelUser, **kwargs: t.Any) -> MembershipPayment:
    return subscription_lifecycle.record_payment(
        sub, amount=Decimal("10.00"), currency="EUR", recorded_by=owner, **kwargs
    )


def _patch(client: Client, org: Organization, payment: MembershipPayment, body: dict[str, t.Any]) -> t.Any:
    url = reverse("api:update_subscription_payment", kwargs={"slug": org.slug, "payment_id": payment.id})
    return client.patch(url, data=orjson.dumps(body), content_type="application/json")


def _void(client: Client, org: Organization, payment: MembershipPayment, body: dict[str, t.Any] | None = None) -> t.Any:
    url = reverse("api:void_subscription_payment", kwargs={"slug": org.slug, "payment_id": payment.id})
    return client.post(url, data=orjson.dumps(body or {}), content_type="application/json")


class TestBackdatedInitialPayment:
    def _create(
        self, client: Client, org: Organization, plan: MembershipSubscriptionPlan, user: RevelUser, **extra: t.Any
    ) -> t.Any:
        payload = {"plan_id": str(plan.id), "user_id": str(user.id), **extra}
        url = reverse("api:create_subscription", kwargs={"slug": org.slug})
        return client.post(url, data=orjson.dumps(payload), content_type="application/json")

    def _pay(self, **kw: t.Any) -> dict[str, t.Any]:
        return {"initial_payment_amount": "10.00", "initial_payment_currency": "EUR", **kw}

    def test_backdated_initial_payment_anchors_period(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        when = timezone.now() - datetime.timedelta(days=10)
        resp = self._create(
            organization_owner_client,
            organization,
            plan,
            subscriber,
            **self._pay(initial_payment_occurred_at=when.isoformat()),
        )
        assert resp.status_code == 201, resp.content
        sub = MembershipSubscription.objects.get(user=subscriber)
        payment = sub.payments.get()
        assert payment.occurred_at == when
        assert payment.period_start == when
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_start == when

    def test_future_is_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        when = timezone.now() + datetime.timedelta(days=1)
        resp = self._create(
            organization_owner_client,
            organization,
            plan,
            subscriber,
            **self._pay(initial_payment_occurred_at=when.isoformat()),
        )
        assert resp.status_code == 400
        assert not MembershipSubscription.objects.filter(user=subscriber).exists()

    def test_lapsed_is_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        when = timezone.now() - datetime.timedelta(days=90)
        resp = self._create(
            organization_owner_client,
            organization,
            plan,
            subscriber,
            **self._pay(initial_payment_occurred_at=when.isoformat()),
        )
        assert resp.status_code == 400
        assert not MembershipSubscription.objects.filter(user=subscriber).exists()

    def test_occurred_at_without_amount_is_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        when = timezone.now() - datetime.timedelta(days=1)
        resp = self._create(
            organization_owner_client, organization, plan, subscriber, initial_payment_occurred_at=when.isoformat()
        )
        assert resp.status_code == 400


class TestUpdatePayment:
    def test_patch_amount_notes_on_non_latest_row(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        _record(sub, organization_owner_user)
        resp = _patch(organization_owner_client, organization, first, {"amount": "12.50", "notes": "typo fixed"})
        assert resp.status_code == 200, resp.content
        first.refresh_from_db()
        assert first.amount == Decimal("12.50")
        assert first.notes == "typo fixed"
        assert resp.json()["can_edit"] is True
        assert resp.json()["can_void"] is False

    def test_empty_body_is_noop(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        payment = sub.payments.get()
        resp = _patch(organization_owner_client, organization, payment, {})
        assert resp.status_code == 200
        assert Decimal(resp.json()["amount"]) == payment.amount

    @pytest.mark.parametrize("body", [{"status": "refunded"}, {"status": "voided"}, {"amount": "-1"}])
    def test_invalid_body_is_422(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        body: dict[str, t.Any],
    ) -> None:
        assert _patch(organization_owner_client, organization, sub.payments.get(), body).status_code == 422

    def test_record_payment_refuses_voided_status(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        url = reverse("api:record_subscription_payment", kwargs={"slug": organization.slug, "sub_id": sub.id})
        body = {"amount": "10.00", "currency": sub.plan.currency, "status": "voided"}
        resp = organization_owner_client.post(url, body, content_type="application/json")
        assert resp.status_code == 422
        assert sub.payments.count() == 1

    def test_status_to_failed_rolls_back_to_previous_row(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        second = _record(sub, organization_owner_user)
        sub.refresh_from_db()
        assert sub.current_period_end == second.period_end
        resp = _patch(organization_owner_client, organization, second, {"status": "failed"})
        assert resp.status_code == 200, resp.content
        sub.refresh_from_db()
        second.refresh_from_db()
        assert second.status == Status.FAILED
        assert sub.current_period_start == first.period_start
        assert sub.current_period_end == first.period_end
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_status_to_failed_without_previous_goes_pending(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        resp = _patch(organization_owner_client, organization, sub.payments.get(), {"status": "pending"})
        assert resp.status_code == 200
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert sub.current_period_start is None
        assert sub.current_period_end is None

    def test_pending_to_succeeded_activates(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        payment = sub.payments.get()
        _patch(organization_owner_client, organization, payment, {"status": "pending"})
        resp = _patch(organization_owner_client, organization, payment, {"status": "succeeded"})
        assert resp.status_code == 200, resp.content
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert sub.current_period_end == MembershipPayment.objects.get(pk=payment.pk).period_end

    def test_occurred_at_recomputes_row_and_subscription(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        payment = sub.payments.get()
        new_when = timezone.now() - datetime.timedelta(days=5)
        resp = _patch(organization_owner_client, organization, payment, {"occurred_at": new_when.isoformat()})
        assert resp.status_code == 200, resp.content
        payment.refresh_from_db()
        sub.refresh_from_db()
        assert payment.occurred_at == new_when
        assert payment.period_start == new_when
        assert sub.current_period_start == new_when
        assert sub.current_period_end == payment.period_end
        assert payment.period_end > new_when + datetime.timedelta(days=27)

    def test_occurred_at_future_is_400(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        when = timezone.now() + datetime.timedelta(days=2)
        resp = _patch(organization_owner_client, organization, sub.payments.get(), {"occurred_at": when.isoformat()})
        assert resp.status_code == 400

    def test_occurred_at_lapsing_is_400(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        when = timezone.now() - datetime.timedelta(days=90)
        resp = _patch(organization_owner_client, organization, sub.payments.get(), {"occurred_at": when.isoformat()})
        assert resp.status_code == 400

    def test_no_notifications(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        second = _record(sub, organization_owner_user)
        before = Notification.objects.count()
        with mock.patch("events.service.subscription.core._dispatch_renewal_succeeded") as renewal:
            assert _patch(organization_owner_client, organization, second, {"status": "failed"}).status_code == 200
            assert _patch(organization_owner_client, organization, second, {"status": "succeeded"}).status_code == 200
            assert _void(organization_owner_client, organization, second).status_code == 200
        renewal.assert_not_called()
        assert Notification.objects.count() == before

    def test_non_staff_forbidden(
        self, member_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        payment = sub.payments.get()
        assert _patch(member_client, organization, payment, {"notes": "x"}).status_code == 403
        assert _void(member_client, organization, payment).status_code == 403


class TestRefusals:
    def _assert_409(self, resp: t.Any, code: str) -> None:
        assert resp.status_code == 409, resp.content
        assert resp.json()["code"] == code
        assert resp.json()["detail"]

    def test_online_plan(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        MembershipSubscriptionPlan.objects.filter(pk=sub.plan_id).update(
            payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE
        )
        self._assert_409(
            _patch(organization_owner_client, organization, sub.payments.get(), {"notes": "x"}), "payment_online"
        )

    @pytest.mark.parametrize("field", ["stripe_invoice_id", "stripe_payment_intent_id"])
    def test_stripe_ids(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription, field: str
    ) -> None:
        payment = sub.payments.get()
        MembershipPayment.objects.filter(pk=payment.pk).update(**{field: "id_123"})
        self._assert_409(_patch(organization_owner_client, organization, payment, {"notes": "x"}), "payment_online")
        self._assert_409(_void(organization_owner_client, organization, payment), "payment_online")

    def test_refunded(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        _record(sub, organization_owner_user)
        first = sub.payments.order_by("created_at").first()
        assert first is not None
        MembershipPayment.objects.filter(pk=first.pk).update(refund_amount=Decimal("1.00"))
        self._assert_409(_patch(organization_owner_client, organization, first, {"notes": "x"}), "payment_refunded")
        MembershipPayment.objects.filter(pk=first.pk).update(refund_amount=None, status=Status.REFUNDED)
        self._assert_409(_patch(organization_owner_client, organization, first, {"notes": "x"}), "payment_refunded")

    def test_voided(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        payment = sub.payments.get()
        assert _void(organization_owner_client, organization, payment).status_code == 200
        self._assert_409(_patch(organization_owner_client, organization, payment, {"notes": "x"}), "payment_voided")
        self._assert_409(_void(organization_owner_client, organization, payment), "payment_voided")

    def test_not_latest(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        _record(sub, organization_owner_user)
        self._assert_409(
            _patch(organization_owner_client, organization, first, {"status": "failed"}), "payment_not_latest"
        )
        when = (timezone.now() - datetime.timedelta(days=3)).isoformat()
        self._assert_409(
            _patch(organization_owner_client, organization, first, {"occurred_at": when}), "payment_not_latest"
        )
        self._assert_409(_void(organization_owner_client, organization, first), "payment_not_latest")

    def test_terminal_subscription(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        payment = sub.payments.get()
        sub.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        sub.save(update_fields=["status"])
        self._assert_409(
            _patch(organization_owner_client, organization, payment, {"status": "failed"}), "subscription_terminal"
        )
        self._assert_409(_void(organization_owner_client, organization, payment), "subscription_terminal")
        # amount/notes stay editable on a terminal subscription.
        assert _patch(organization_owner_client, organization, payment, {"notes": "ok"}).status_code == 200

    def test_same_status_is_not_a_period_change(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        _record(sub, organization_owner_user)
        resp = _patch(organization_owner_client, organization, first, {"status": "succeeded", "notes": "same"})
        assert resp.status_code == 200


class TestVoid:
    def test_void_rolls_back_and_appends_notes(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        MembershipPayment.objects.filter(pk=first.pk).update(notes="orig")
        second = _record(sub, organization_owner_user, notes="dup")
        resp = _void(organization_owner_client, organization, second, {"notes": "entered twice"})
        assert resp.status_code == 200, resp.content
        assert resp.json()["status"] == "voided"
        second.refresh_from_db()
        sub.refresh_from_db()
        assert second.notes == "dup\nentered twice"
        assert sub.current_period_end == first.period_end
        assert sub.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_void_without_body(
        self, organization_owner_client: Client, organization: Organization, sub: MembershipSubscription
    ) -> None:
        url = reverse(
            "api:void_subscription_payment", kwargs={"slug": organization.slug, "payment_id": sub.payments.get().id}
        )
        resp = organization_owner_client.post(url, content_type="application/json")
        assert resp.status_code == 200, resp.content
        sub.refresh_from_db()
        assert sub.status == MembershipSubscription.SubscriptionStatus.PENDING
        assert sub.current_period_end is None

    def test_void_then_next_latest_is_voidable(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        second = _record(sub, organization_owner_user)
        assert _void(organization_owner_client, organization, second).status_code == 200
        assert _void(organization_owner_client, organization, first).status_code == 200

    def test_lists_exclude_voided_by_default(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        _record(sub, organization_owner_user)
        voided = _record(sub, organization_owner_user)
        _void(organization_owner_client, organization, voided)

        per_sub = reverse("api:list_subscription_payments", kwargs={"slug": organization.slug, "sub_id": sub.id})
        org_wide = reverse("api:list_organization_subscription_payments", kwargs={"slug": organization.slug})
        for url in (per_sub, org_wide):
            assert organization_owner_client.get(url).json()["count"] == 2
            body = organization_owner_client.get(url, {"include_voided": "true"}).json()
            assert body["count"] == 3
            assert {r["status"] for r in body["results"]} >= {"voided"}
        assert organization_owner_client.get(org_wide, {"status": "voided"}).json()["count"] == 1

    def test_me_list_excludes_voided(
        self, sub: MembershipSubscription, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        from ninja_jwt.tokens import RefreshToken

        extra = _record(sub, organization_owner_user)
        MembershipPayment.objects.filter(pk=extra.pk).update(status=Status.VOIDED)
        token = RefreshToken.for_user(sub.user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")  # type: ignore[attr-defined]
        url = reverse("api:list_my_membership_payments", kwargs={"org_id": organization.id})
        data = client.get(url).json()
        assert data["count"] == 1
        assert all(r["status"] != "voided" for r in data["results"])

    def test_revenue_excludes_voided(
        self, organization: Organization, sub: MembershipSubscription, organization_owner_user: RevelUser
    ) -> None:
        voided = _record(sub, organization_owner_user, notes="v")
        MembershipPayment.objects.filter(pk=voided.pk).update(status=Status.VOIDED)
        today = timezone.now().date()
        scope = revenue_aggregation.ReportScope(
            org=organization, event_id=None, date_from=today - datetime.timedelta(days=30), date_to=today
        )
        fin = revenue_aggregation.organization_financials(scope, currency=None, sort="revenue", order="desc")
        assert [m.payment_count for m in fin.memberships] == [1]
        assert fin.memberships[0].gross == Decimal("10.00")


class TestFlags:
    def test_can_edit_and_can_void_in_lists(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        first = sub.payments.get()
        second = _record(sub, organization_owner_user)
        online = _record(sub, organization_owner_user)
        MembershipPayment.objects.filter(pk=online.pk).update(stripe_invoice_id="in_1")
        url = reverse("api:list_subscription_payments", kwargs={"slug": organization.slug, "sub_id": sub.id})
        with_flags = {
            r["id"]: (r["can_edit"], r["can_void"]) for r in organization_owner_client.get(url).json()["results"]
        }
        # ``online`` is the latest row but ineligible; ``second`` is editable yet not latest.
        assert with_flags[str(online.id)] == (False, False)
        assert with_flags[str(second.id)] == (True, False)
        assert with_flags[str(first.id)] == (True, False)

    def test_latest_row_flags_and_no_n_plus_one(
        self,
        organization_owner_client: Client,
        organization: Organization,
        sub: MembershipSubscription,
        organization_owner_user: RevelUser,
    ) -> None:
        url = reverse("api:list_organization_subscription_payments", kwargs={"slug": organization.slug})
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as small:
            body = organization_owner_client.get(url).json()
        assert body["results"][0]["can_edit"] is True
        assert body["results"][0]["can_void"] is True
        for _ in range(4):
            _record(sub, organization_owner_user)
        with CaptureQueriesContext(connection) as large:
            organization_owner_client.get(url)
        assert len(large) == len(small)
