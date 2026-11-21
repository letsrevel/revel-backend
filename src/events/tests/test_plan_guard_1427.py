"""Plan currency/cadence guard beyond the API path (#1427).

Covers the model-level guard (Django admin, history revert, shell), the
creation race against ``update_plan``, and ``migrate_plan_subscribers``
racing a member's own plan change.
"""

import threading
import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.admin.sites import site as admin_site
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ninja.errors import HttpError
from pytest_django.fixtures import Settings

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription import plans as subscription_plans
from events.service.subscription.sales import ensure_plan_sales_capacity

Plan = MembershipSubscriptionPlan
Sub = MembershipSubscription


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def plan(tier: MembershipTier) -> Plan:
    return Plan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.MONTH,
        payment_method=Plan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="g_user", email="g_user@example.com", password="pass")


def _make_sub(
    user: RevelUser,
    plan: Plan,
    organization: Organization,
    *,
    status: str = Sub.SubscriptionStatus.ACTIVE,
    **extra: t.Any,
) -> Sub:
    return Sub.objects.create(user=user, plan=plan, organization=organization, status=status, **extra)


@pytest.mark.django_db
class TestPlanModelGuard:
    """``clean()`` refuses currency/cadence changes against the persisted row while subscribers exist."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [("currency", "USD"), ("period_unit", Plan.PeriodUnit.YEAR), ("period_count", 3)],
    )
    def test_change_refused_with_live_subscription(
        self, plan: Plan, organization: Organization, subscriber: RevelUser, field: str, value: t.Any
    ) -> None:
        _make_sub(subscriber, plan, organization, status=Sub.SubscriptionStatus.PAST_DUE)
        setattr(plan, field, value)
        with pytest.raises(ValidationError) as exc_info:
            plan.save()
        assert field in exc_info.value.message_dict
        assert Plan.objects.values_list(field, flat=True).get(pk=plan.pk) != value

    def test_change_refused_when_downgrade_scheduled_into_plan(
        self, plan: Plan, tier: MembershipTier, organization: Organization, subscriber: RevelUser
    ) -> None:
        other = Plan.objects.create(
            tier=tier,
            name="Yearly",
            price=Decimal("100"),
            currency="EUR",
            period_unit=Plan.PeriodUnit.YEAR,
            payment_method=Plan.PaymentMethod.OFFLINE,
        )
        _make_sub(subscriber, other, organization, pending_plan=plan)
        plan.period_unit = Plan.PeriodUnit.YEAR
        with pytest.raises(ValidationError):
            plan.save()

    def test_change_allowed_without_subscriptions(self, plan: Plan) -> None:
        plan.period_unit = Plan.PeriodUnit.YEAR
        plan.currency = "USD"
        plan.save()

    def test_change_allowed_with_only_terminal_subscriptions(
        self, plan: Plan, organization: Organization, subscriber: RevelUser
    ) -> None:
        _make_sub(subscriber, plan, organization, status=Sub.SubscriptionStatus.CANCELLED)
        plan.period_count = 3
        plan.save()

    def test_unchanged_cadence_allowed_with_live_subscription(
        self, plan: Plan, organization: Organization, subscriber: RevelUser
    ) -> None:
        _make_sub(subscriber, plan, organization)
        plan.name = "Renamed"
        plan.currency = "eur"  # case-only difference is not a currency change
        plan.save()

    def test_lifetime_period_count_edit_allowed(
        self, plan: Plan, organization: Organization, subscriber: RevelUser
    ) -> None:
        Plan.objects.filter(pk=plan.pk).update(period_unit=Plan.PeriodUnit.LIFETIME)
        plan.refresh_from_db()
        _make_sub(subscriber, plan, organization)
        plan.period_count = 5
        plan.save()

    def test_new_plan_unaffected(self, tier: MembershipTier) -> None:
        Plan.objects.create(
            tier=tier,
            name="Fresh",
            price=Decimal("5"),
            currency="EUR",
            period_unit=Plan.PeriodUnit.YEAR,
            payment_method=Plan.PaymentMethod.OFFLINE,
        )

    def test_history_revert_refused(self, plan: Plan, organization: Organization, subscriber: RevelUser) -> None:
        """A simple_history revert restores an old cadence through ``save()``."""
        plan.period_unit = Plan.PeriodUnit.YEAR
        plan.save()  # no subscribers yet: allowed, creates a history row
        _make_sub(subscriber, plan, organization)
        old = plan.history.order_by("history_date").first()
        assert old is not None
        reverted = old.instance
        reverted._state.adding = False  # what simple_history's admin revert does before saving
        with pytest.raises(ValidationError) as exc_info:
            reverted.save()
        assert "period_unit" in exc_info.value.message_dict

    def test_update_fields_save_without_billing_fields_skips_guard(
        self, plan: Plan, organization: Organization, subscriber: RevelUser
    ) -> None:
        """A stale instance can still toggle ``is_active``; a full save of it is refused."""
        stale = Plan.objects.get(pk=plan.pk)
        Plan.objects.filter(pk=plan.pk).update(period_unit=Plan.PeriodUnit.YEAR)
        _make_sub(subscriber, plan, organization)
        stale.is_active = False
        stale.save(update_fields=["is_active"])
        assert Plan.objects.values_list("is_active", flat=True).get(pk=plan.pk) is False
        with pytest.raises(ValidationError):
            stale.save()
        with pytest.raises(ValidationError):
            stale.save(update_fields=["is_active", "period_unit"])

    def test_guard_reads_persisted_row_for_update(
        self, plan: Plan, organization: Organization, subscriber: RevelUser
    ) -> None:
        _make_sub(subscriber, plan, organization)
        plan.currency = "USD"
        with CaptureQueriesContext(connection) as ctx, pytest.raises(ValidationError):
            plan.save()
        assert any(
            "FOR UPDATE" in q["sql"] and "events_membershipsubscriptionplan" in q["sql"] for q in ctx.captured_queries
        )


@pytest.fixture
def no_manifest_storage(settings: Settings) -> None:
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.mark.django_db
def test_admin_change_form_cannot_change_cadence_of_plan_with_members(
    no_manifest_storage: None,
    client: Client,
    superuser: RevelUser,
    plan: Plan,
    organization: Organization,
    subscriber: RevelUser,
) -> None:
    _make_sub(subscriber, plan, organization)
    client.force_login(superuser)
    url = reverse("admin:events_membershipsubscriptionplan_change", args=[plan.pk])
    response = client.get(url)
    assert response.status_code == 200
    form = response.context["adminform"].form
    data = {k: ("" if v is None else v) for k, v in form.initial.items()}
    data["period_unit"] = Plan.PeriodUnit.YEAR
    data["_save"] = "Save"
    response = client.post(url, data)
    # Re-rendered with a form error instead of redirecting: the save was refused.
    assert response.status_code == 200
    assert "period_unit" in response.context["adminform"].form.errors
    plan.refresh_from_db()
    assert plan.period_unit == Plan.PeriodUnit.MONTH
    assert admin_site.is_registered(Plan)


@pytest.mark.django_db(transaction=True)
def test_subscription_insert_blocks_concurrent_cadence_change(
    plan: Plan, organization: Organization, subscriber: RevelUser
) -> None:
    """A subscription created while ``update_plan`` is deciding cannot slip past the guard.

    Subscription creation locks the plan row (``ensure_plan_sales_capacity``:
    ``FOR KEY SHARE`` on an uncapped plan) before inserting, and ``update_plan``
    takes ``FOR UPDATE``, which conflicts, so whichever transaction is second
    waits for the first, and ``update_plan`` either sees the committed
    subscription or finishes before it exists. A bare
    FK insert would not serialize: Django's FK constraints are DEFERRABLE
    INITIALLY DEFERRED, so the ``FOR KEY SHARE`` is only taken at commit.

    ``transaction=True`` is required: the two threads use their own connections
    and must see each other's real commits, which the rollback-wrapped default
    ``django_db`` transaction (one connection, never committed) cannot provide.
    """
    inserted = threading.Event()
    release = threading.Event()
    outcome: dict[str, t.Any] = {}

    def insert_sub() -> None:
        try:
            with transaction.atomic():
                ensure_plan_sales_capacity(plan)  # the lock every creation path takes
                _make_sub(subscriber, plan, organization, status=Sub.SubscriptionStatus.PENDING)
                inserted.set()
                release.wait(timeout=20)
        finally:
            inserted.set()
            close_old_connections()

    def change_cadence() -> None:
        try:
            subscription_plans.update_plan(plan, period_unit=Plan.PeriodUnit.YEAR)
            outcome["result"] = "changed"
        except HttpError as exc:
            outcome["result"] = exc.status_code
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["result"] = repr(exc)
        finally:
            close_old_connections()

    thread_a = threading.Thread(target=insert_sub, daemon=True)
    thread_b = threading.Thread(target=change_cadence, daemon=True)
    try:
        thread_a.start()
        assert inserted.wait(timeout=10), "subscription insert did not start"
        thread_b.start()
        thread_b.join(timeout=1.5)
        waited = thread_b.is_alive()
    finally:
        release.set()
        for worker in (thread_a, thread_b):
            if worker.ident is not None:
                worker.join(timeout=10)
    assert waited, "update_plan did not wait for the in-flight subscription insert"
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert outcome["result"] == 400
    plan.refresh_from_db()
    assert plan.period_unit == Plan.PeriodUnit.MONTH


@pytest.mark.django_db(transaction=True)
def test_concurrent_creations_on_uncapped_plan_do_not_serialize(plan: Plan) -> None:
    """Uncapped plans take a shared lock, so one checkout's open transaction never blocks another's.

    Under ATOMIC_REQUESTS the creation lock is held across the Checkout-session
    Stripe call; an exclusive lock there would queue every subscriber of a plan.
    ``transaction=True``: the two threads need their own connections and real
    concurrent transactions.
    """
    assert plan.max_subscriptions is None
    holding = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    errors: dict[str, BaseException] = {}

    def first() -> None:
        try:
            with transaction.atomic():
                ensure_plan_sales_capacity(plan)
                holding.set()  # only after the lock is really held
                release.wait(timeout=20)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors["first"] = exc
        finally:
            close_old_connections()

    def second() -> None:
        try:
            with transaction.atomic():
                ensure_plan_sales_capacity(plan)
                second_done.set()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors["second"] = exc
        finally:
            close_old_connections()

    thread_a = threading.Thread(target=first, daemon=True)
    thread_b = threading.Thread(target=second, daemon=True)
    try:
        thread_a.start()
        held = holding.wait(timeout=10)
        if held:
            thread_b.start()
        overlapped = held and second_done.wait(timeout=5)
    finally:
        release.set()
        for worker in (thread_a, thread_b):
            if worker.ident is not None:
                worker.join(timeout=10)
    assert not errors
    assert held, "the first creation never took its lock"
    assert overlapped, "a second creation waited for the first: uncapped plans must not serialize"


@pytest.fixture
def online_plan(tier: MembershipTier) -> Plan:
    return Plan.objects.create(
        tier=tier,
        name="OnlineMonthly",
        price=Decimal("10"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.MONTH,
        payment_method=Plan.PaymentMethod.ONLINE,
        stripe_product_id="prod_x",
        stripe_price_id="price_new",
    )


@pytest.mark.django_db
class TestMigrateRecheckUnderLock:
    """A member's own plan change between the batch query and the swap must not be overwritten."""

    def _setup_sub(self, online_plan: Plan, organization: Organization, subscriber: RevelUser) -> Sub:
        return _make_sub(subscriber, online_plan, organization, stripe_subscription_id="sub_x")

    def _run(
        self, online_plan: Plan, organization_owner_user: RevelUser, sub: Sub, mutate: t.Callable[[], t.Any]
    ) -> tuple[subscription_plans.MigrationResult, t.Any]:
        """Run the migration, applying ``mutate`` after the batch query but before the first swap."""
        real_atomic = transaction.atomic
        fired = {"done": False}

        def atomic_then_mutate(*args: t.Any, **kwargs: t.Any) -> t.Any:
            if not fired["done"]:
                fired["done"] = True
                mutate()
            return real_atomic(*args, **kwargs)

        with (
            patch("events.service.subscription.plans.transaction.atomic", side_effect=atomic_then_mutate),
            patch("events.service.subscription.stripe.checkout.update_subscription_price") as mock_update,
        ):
            result = subscription_plans.migrate_plan_subscribers(online_plan, initiated_by=organization_owner_user)
        return result, mock_update

    def test_skips_sub_whose_plan_changed(
        self,
        online_plan: Plan,
        tier: MembershipTier,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        other = Plan.objects.create(
            tier=tier,
            name="Other",
            price=Decimal("20"),
            currency="EUR",
            period_unit=Plan.PeriodUnit.MONTH,
            payment_method=Plan.PaymentMethod.OFFLINE,
        )
        sub = self._setup_sub(online_plan, organization, subscriber)
        result, mock_update = self._run(
            online_plan, organization_owner_user, sub, lambda: Sub.objects.filter(pk=sub.pk).update(plan=other)
        )
        mock_update.assert_not_called()
        assert result["skipped"] == 1
        assert result["migrated"] == 0

    def test_skips_sub_that_gained_a_schedule(
        self,
        online_plan: Plan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        sub = self._setup_sub(online_plan, organization, subscriber)
        result, mock_update = self._run(
            online_plan,
            organization_owner_user,
            sub,
            lambda: Sub.objects.filter(pk=sub.pk).update(stripe_schedule_id="sub_sched_1"),
        )
        mock_update.assert_not_called()
        assert result["skipped_schedule_managed"] == 1
        assert result["migrated"] == 0

    def test_skips_sub_that_went_terminal(
        self,
        online_plan: Plan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        sub = self._setup_sub(online_plan, organization, subscriber)
        result, mock_update = self._run(
            online_plan,
            organization_owner_user,
            sub,
            lambda: Sub.objects.filter(pk=sub.pk).update(status=Sub.SubscriptionStatus.CANCELLED),
        )
        mock_update.assert_not_called()
        assert result["skipped"] == 1
        assert result["migrated"] == 0

    def test_swaps_unchanged_sub_under_row_lock(
        self,
        online_plan: Plan,
        organization: Organization,
        subscriber: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        sub = self._setup_sub(online_plan, organization, subscriber)
        with CaptureQueriesContext(connection) as ctx:
            result, mock_update = self._run(online_plan, organization_owner_user, sub, lambda: None)
        mock_update.assert_called_once()
        assert result["skipped"] == 0
        assert any('FOR UPDATE OF "events_membershipsubscription"' in q["sql"] for q in ctx.captured_queries)


@pytest.mark.django_db
def test_subscription_admin_cannot_change_plan_of_existing_subscription(
    no_manifest_storage: None,
    client: Client,
    superuser: RevelUser,
    plan: Plan,
    tier: MembershipTier,
    organization: Organization,
    subscriber: RevelUser,
) -> None:
    other = Plan.objects.create(
        tier=tier,
        name="Other",
        price=Decimal("20"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.MONTH,
        payment_method=Plan.PaymentMethod.OFFLINE,
    )
    sub = _make_sub(subscriber, plan, organization)
    client.force_login(superuser)
    url = reverse("admin:events_membershipsubscription_change", args=[sub.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert "plan" not in response.context["adminform"].form.fields
    form = response.context["adminform"].form
    data = {k: ("" if v is None else v) for k, v in form.initial.items() if k in form.fields}
    # Forged `plan` key: a writable plan field would honour it; readonly ignores it.
    data["plan"] = str(other.pk)
    data["_save"] = "Save"
    response = client.post(url, data)
    # A 302 proves the save succeeded (a form error re-renders with 200), so the check below can fail.
    assert response.status_code == 302
    sub.refresh_from_db()
    assert sub.plan_id == plan.pk


def _run_stale_capacity_check(plan: Plan, change: t.Callable[[], t.Any]) -> tuple[Plan, dict[str, t.Any]]:
    """Thread A holds the plan row ``FOR UPDATE`` and applies ``change``; thread B checks capacity on a stale copy.

    Asserts B blocks until A commits, and returns B's instance and outcome.
    """
    locked = threading.Event()
    release = threading.Event()
    outcome: dict[str, t.Any] = {}
    stale = Plan.objects.select_related("tier__organization").get(pk=plan.pk)

    def editor() -> None:
        try:
            with transaction.atomic():
                Plan.objects.select_for_update().get(pk=plan.pk)
                change()
                locked.set()
                release.wait(timeout=20)
        finally:
            locked.set()
            close_old_connections()

    def creator() -> None:
        try:
            with transaction.atomic():
                ensure_plan_sales_capacity(stale)
            outcome["result"] = "ok"
        except HttpError as exc:
            outcome["result"] = exc.status_code
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion in the test
            outcome["result"] = repr(exc)
        finally:
            close_old_connections()

    thread_a = threading.Thread(target=editor, daemon=True)
    thread_b = threading.Thread(target=creator, daemon=True)
    try:
        thread_a.start()
        assert locked.wait(timeout=10), "editor did not take the lock"
        thread_b.start()
        thread_b.join(timeout=1.5)
        waited = thread_b.is_alive()
    finally:
        release.set()
        for worker in (thread_a, thread_b):
            if worker.ident is not None:
                worker.join(timeout=10)
    assert waited, "capacity check did not wait for the concurrent plan edit"
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    return stale, outcome


@pytest.mark.django_db(transaction=True)
def test_capacity_check_refreshes_stale_plan_after_waiting_out_an_edit(plan: Plan) -> None:
    """The caller's plan instance shows the values committed by the edit it waited for.

    ``transaction=True``: two threads on their own connections must see each
    other's real commits and lock waits, which the rollback-wrapped default
    ``django_db`` transaction cannot provide.
    """
    stale, outcome = _run_stale_capacity_check(
        plan,
        lambda: Plan.objects.filter(pk=plan.pk).update(
            currency="USD", price=Decimal("99"), stripe_price_id="price_after"
        ),
    )
    assert outcome["result"] == "ok"
    assert (stale.currency, stale.price, stale.stripe_price_id) == ("USD", Decimal("99"), "price_after")
    # Only columns are refreshed: the cached tier relation survives (no extra queries on the checkout path).
    assert Plan.tier.is_cached(stale)


@pytest.mark.django_db(transaction=True)
def test_capacity_check_honours_cap_set_while_waiting(
    plan: Plan, organization: Organization, subscriber: RevelUser
) -> None:
    """An uncapped plan that gained a cap during the wait is counted against that cap.

    ``transaction=True`` for the same reason as the refresh test: real
    concurrent connections.
    """
    _make_sub(subscriber, plan, organization)
    stale, outcome = _run_stale_capacity_check(
        plan, lambda: Plan.objects.filter(pk=plan.pk).update(max_subscriptions=1)
    )
    assert outcome["result"] == 400
    assert stale.max_subscriptions == 1
