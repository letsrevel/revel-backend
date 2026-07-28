"""Empirical probe: does a scheduled downgrade keep the Connect platform fee? (#821).

``subscription_stripe_plan_change._downgrade_online_subscription`` creates a
``stripe.SubscriptionSchedule`` with ``from_subscription=...`` and then
*replaces* the whole ``phases`` array with hand-built phases that carry no
``application_fee_percent`` — and it never passes ``default_settings``. Whether
the platform fee survives that rewrite hinges entirely on whether Stripe copies
the source subscription's ``application_fee_percent`` into the schedule's
``default_settings`` at create time. That is not something the docs settle, so
this test settles it against the real API.

Everything here runs against the real Stripe test-mode API, at the real pinned
version (``settings.STRIPE_API_VERSION``; the pin is applied by
``stripe.api_version = ...`` in the service modules, so importing them is
enough), on the real connected test account — application fees only exist on
Connect, hence the ``CONNECTED_TEST_STRIPE_ID`` skip guard on the whole module.

Three pieces of evidence are gathered:

① a pure API probe: what ``SubscriptionSchedule.create(from_subscription=...)``
   returns in ``default_settings.application_fee_percent`` (the schedule is
   released again so the subscription is unmanaged for step ②);
② the real production function's schedule: ``default_settings`` plus every
   phase's ``application_fee_percent`` after the phases rewrite;
③ the underlying subscription's own ``application_fee_percent`` afterwards.

All values are collected and printed *before* any assertion, so a run always
produces the full picture. A FAILURE here means bug #821 is real: the platform
fee is silently dropped on every renewal following a downgrade.

Run manually with:
    pytest -m integration src/events/tests/test_service/test_stripe_schedule_fee_integration.py -v -s

Requires real test-mode keys in .env (``STRIPE_SECRET_KEY=sk_test_…``) and a
real connected test account (``CONNECTED_TEST_STRIPE_ID``).
"""

import json
import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import stripe
from django.conf import settings
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_stripe_plan_change

pytestmark = [
    pytest.mark.integration,
    pytest.mark.django_db,
    pytest.mark.skipif(
        settings.STRIPE_SECRET_KEY in {"sk_test_...", ""},
        reason="needs real Stripe test-mode keys in .env",
    ),
    pytest.mark.skipif(
        not settings.CONNECTED_TEST_STRIPE_ID,
        reason="application fees only exist on Connect: needs CONNECTED_TEST_STRIPE_ID in .env",
    ),
]

# The fee the source subscription is created with, and therefore the fee every
# renewal after the downgrade must still collect.
FEE_PERCENT = 1.8

CURRENT_PRICE_CENTS = 1000
CHEAPER_PRICE_CENTS = 700


class _Sandbox(t.NamedTuple):
    """Ids of the throwaway Stripe objects created on the connected test account."""

    product_id: str
    current_price_id: str
    cheaper_price_id: str
    customer_id: str
    subscription_id: str


def _account_kwargs() -> dict[str, str]:
    """Return the ``stripe_account=`` header kwargs for the connected test account."""
    return {"stripe_account": t.cast(str, settings.CONNECTED_TEST_STRIPE_ID)}


def _fee_of(container: t.Any) -> t.Any:
    """Return ``application_fee_percent`` out of a Stripe sub-object, or None when absent."""
    return (container or {}).get("application_fee_percent")


def _phase_fees(schedule: t.Any) -> list[t.Any]:
    """Return each schedule phase's ``application_fee_percent`` (None where unset)."""
    return [phase.get("application_fee_percent") for phase in (schedule.get("phases") or [])]


def _safely(label: str, call: t.Callable[[], t.Any]) -> None:
    """Run one best-effort teardown call, reporting (not raising) Stripe failures."""
    try:
        call()
    except stripe.error.StripeError as exc:
        print(f"[#821 cleanup] {label} failed, ignored: {exc}")  # noqa: T201


def _teardown(sandbox: _Sandbox) -> None:
    """Best-effort cleanup of the connected sandbox account, one guarded call at a time."""
    kwargs = _account_kwargs()

    def _release_any_schedule() -> None:
        # Releasing an already-released schedule errors, so read the live link first.
        live = stripe.Subscription.retrieve(sandbox.subscription_id, **kwargs)
        schedule = live.get("schedule")
        schedule_id = schedule.get("id") if isinstance(schedule, dict) else schedule
        if schedule_id:
            stripe.SubscriptionSchedule.release(schedule_id, **kwargs)  # type: ignore[arg-type]

    _safely("release schedule", _release_any_schedule)
    _safely("cancel subscription", lambda: stripe.Subscription.cancel(sandbox.subscription_id, **kwargs))  # type: ignore[attr-defined]
    _safely("delete customer", lambda: stripe.Customer.delete(sandbox.customer_id, **kwargs))
    _safely("deactivate current price", lambda: stripe.Price.modify(sandbox.current_price_id, active=False, **kwargs))
    _safely("deactivate cheaper price", lambda: stripe.Price.modify(sandbox.cheaper_price_id, active=False, **kwargs))
    _safely("archive product", lambda: stripe.Product.modify(sandbox.product_id, active=False, **kwargs))


@pytest.fixture
def stripe_sandbox() -> t.Iterator[_Sandbox]:
    """Provision a product, two monthly EUR prices, a paying customer and a fee-bearing subscription.

    The subscription is a Connect *direct charge* (platform key + Stripe-Account
    header, which ``stripe_account=`` supplies) carrying
    ``application_fee_percent=1.8`` — the fee whose survival this module probes.

    Yields:
        The ids of every object created, for the test and the teardown.
    """
    kwargs = _account_kwargs()
    suffix = uuid.uuid4().hex[:8]
    product = stripe.Product.create(name=f"Revel #821 probe {suffix}", **kwargs)
    current_price = stripe.Price.create(
        product=product.id,
        unit_amount=CURRENT_PRICE_CENTS,
        currency="eur",
        recurring={"interval": "month"},
        **kwargs,
    )
    cheaper_price = stripe.Price.create(
        product=product.id,
        unit_amount=CHEAPER_PRICE_CENTS,
        currency="eur",
        recurring={"interval": "month"},
        **kwargs,
    )
    customer = stripe.Customer.create(
        name=f"Revel #821 probe {suffix}",
        email=f"revel-821-{suffix}@example.com",
        **kwargs,
    )
    # Attaching a shared test payment method returns a customer-scoped clone, so
    # the default must be set from the *returned* id, not the "pm_card_visa" alias.
    # The stub only exposes the instance overload of ``attach`` (the static one is
    # hidden behind ``@class_method_variant``); the runtime API takes the id string.
    attached = stripe.PaymentMethod.attach("pm_card_visa", customer=customer.id, **kwargs)  # type: ignore[type-var]
    payment_method = t.cast(stripe.PaymentMethod, attached)
    stripe.Customer.modify(
        customer.id,
        invoice_settings={"default_payment_method": payment_method.id},
        **kwargs,
    )
    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{"price": current_price.id}],
        application_fee_percent=FEE_PERCENT,
        **kwargs,
    )
    sandbox = _Sandbox(
        product_id=product.id,
        current_price_id=current_price.id,
        cheaper_price_id=cheaper_price.id,
        customer_id=customer.id,
        subscription_id=subscription.id,
    )
    try:
        assert subscription.status == "active", f"sandbox subscription did not activate: {subscription.status}"
        assert subscription.get("application_fee_percent") == FEE_PERCENT, (
            f"sandbox subscription lost its fee at creation: {subscription.get('application_fee_percent')!r}"
        )
        yield sandbox
    finally:
        _teardown(sandbox)


@pytest.fixture
def downgrade_rows(
    organization: Organization,
    member_user: RevelUser,
    stripe_sandbox: _Sandbox,
) -> tuple[MembershipSubscription, MembershipSubscriptionPlan]:
    """Build the minimal ORM rows ``_downgrade_online_subscription`` reads.

    The two plans point at the two real Stripe prices from the sandbox, and the
    organization is pointed at the real connected test account so
    ``_stripe_account_kwargs`` emits the right Stripe-Account header.

    Returns:
        The ACTIVE subscription on the pricier plan, and the cheaper target plan.
    """
    organization.stripe_account_id = settings.CONNECTED_TEST_STRIPE_ID
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(
        update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted", "updated_at"]
    )
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    current_plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Standard Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id=stripe_sandbox.product_id,
        stripe_price_id=stripe_sandbox.current_price_id,
    )
    cheaper_plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Lite Monthly",
        price=Decimal("7.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id=stripe_sandbox.product_id,
        stripe_price_id=stripe_sandbox.cheaper_price_id,
    )
    subscription = MembershipSubscription.objects.create(
        user=member_user,
        plan=current_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id=stripe_sandbox.subscription_id,
        current_period_end=timezone.now() + timedelta(days=30),
    )
    return subscription, cheaper_plan


def test_scheduled_downgrade_keeps_application_fee_percent(
    stripe_sandbox: _Sandbox,
    downgrade_rows: tuple[MembershipSubscription, MembershipSubscriptionPlan],
) -> None:
    """Walk ①→②→③ and assert the platform fee still applies after a scheduled downgrade.

    Sequential by necessity: the probe schedule of ① must be released before ②
    can create its own schedule from the same subscription.
    """
    subscription, new_plan = downgrade_rows
    kwargs = _account_kwargs()
    evidence: dict[str, t.Any] = {
        "stripe_api_version": settings.STRIPE_API_VERSION,
        "connected_account": settings.CONNECTED_TEST_STRIPE_ID,
        "source_subscription_id": stripe_sandbox.subscription_id,
        "source_fee_percent": FEE_PERCENT,
    }

    # ① Pure API probe: does `from_subscription` seed `default_settings` with the fee?
    probe = stripe.SubscriptionSchedule.create(from_subscription=stripe_sandbox.subscription_id, **kwargs)
    evidence["probe_schedule_id"] = probe.id
    evidence["probe_default_settings_fee"] = _fee_of(probe.get("default_settings"))
    evidence["probe_phase_fees"] = _phase_fees(probe)
    # Release so the subscription is unmanaged again and ② can create its own schedule.
    stripe.SubscriptionSchedule.release(probe.id, **kwargs)  # type: ignore[arg-type]

    # ② The real production code path, unmocked, against the real API.
    subscription_stripe_plan_change._downgrade_online_subscription(subscription, new_plan)
    subscription.refresh_from_db()
    evidence["code_path_schedule_id"] = subscription.stripe_schedule_id
    evidence["pending_plan_id"] = str(subscription.pending_plan_id)
    schedule = stripe.SubscriptionSchedule.retrieve(subscription.stripe_schedule_id, **kwargs)
    evidence["schedule_default_settings_fee"] = _fee_of(schedule.get("default_settings"))
    evidence["schedule_phase_fees"] = _phase_fees(schedule)

    # ③ What the live subscription itself now says about the fee.
    live_subscription = stripe.Subscription.retrieve(stripe_sandbox.subscription_id, **kwargs)
    evidence["subscription_fee_after_downgrade"] = live_subscription.get("application_fee_percent")

    print("\n#821 evidence:", json.dumps(evidence, indent=2, default=str))  # noqa: T201

    phase_fees = evidence["schedule_phase_fees"]
    final_phase_fee = phase_fees[-1] if phase_fees else None
    # A phase without its own fee inherits the schedule's default_settings.
    renewal_fee = final_phase_fee if final_phase_fee is not None else evidence["schedule_default_settings_fee"]

    assert renewal_fee == FEE_PERCENT, (
        "Bug #821 is REAL: the hand-built phases dropped the platform fee. Renewals after the scheduled "
        f"downgrade would collect application_fee_percent={renewal_fee!r} instead of {FEE_PERCENT}. "
        f"Evidence: {evidence}"
    )
    assert evidence["subscription_fee_after_downgrade"] == FEE_PERCENT, (
        "Bug #821 is REAL: the schedule stripped application_fee_percent off the live subscription "
        f"({evidence['subscription_fee_after_downgrade']!r} != {FEE_PERCENT}). Evidence: {evidence}"
    )
