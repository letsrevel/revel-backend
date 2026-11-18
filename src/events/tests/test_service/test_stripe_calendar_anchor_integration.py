"""Canary: Stripe still honours the calendar-plan billing-cycle anchor the way #1426 relies on.

Replaces the throwaway #1424 spike scripts. Against the real Stripe test-mode API, at the pinned
version (``settings.STRIPE_API_VERSION``), on the real connected test account:

1. Checkout accepts the params ``_create_subscription_checkout_session`` sends for a calendar plan
   (``billing_cycle_anchor`` + ``proration_behavior=create_prorations``).
2. A subscription created with those params yields the documented first-invoice shape (one prorated
   line, price = the plan's price, period join -> boundary), a subscription period ending on the
   boundary, and takes the application fee. The invoice is then replayed through
   ``record_stripe_payment_from_invoice`` / ``sync_subscription_from_stripe`` to prove the ledger
   row's period matches the subscription mirror (what the refund auto-cancel compares).

3. The free tiny stub (``proration_behavior="none"``): Checkout accepts it, a subscription created with it
   opens no invoice and renews on the boundary, and the below-minimum prorated stub Stripe would
   otherwise create is the documented zero-paid shape (``amount_paid`` 0, no PaymentIntent) that sync
   records as a zero ledger row without alarms.

If this fails, the ONLINE calendar flow no longer matches Stripe: Stripe changed an API shape.

Run manually with:
    pytest -m integration src/events/tests/test_service/test_stripe_calendar_anchor_integration.py -v -s

Requires real test-mode keys in .env (``STRIPE_SECRET_KEY=sk_test_...``) and a real connected test
account (``CONNECTED_TEST_STRIPE_ID``).
"""

import datetime
import typing as t
import uuid
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from django.conf import settings
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    CustomerProfile,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.subscription.stripe import checkout as subscription_stripe_checkout
from events.service.subscription.stripe import sync as subscription_stripe_sync
from events.service.subscription.stripe.payloads import StripeAccountKwargs
from events.utils.subscription_periods import first_period_end, is_free_stub

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

FEE_PERCENT = 1.8
PRICE_CENTS = 1200


class _Sandbox(t.NamedTuple):
    """Ids of the throwaway Stripe objects created on the connected test account."""

    product_id: str
    price_id: str
    customer_id: str


def _account_kwargs() -> StripeAccountKwargs:
    return {"stripe_account": t.cast(str, settings.CONNECTED_TEST_STRIPE_ID)}


def _safely(label: str, call: t.Callable[[], t.Any]) -> None:
    try:
        call()
    except stripe.error.StripeError as exc:
        print(f"[#1426 cleanup] {label} failed, ignored: {exc}")  # noqa: T201


@pytest.fixture
def sandbox() -> t.Iterator[_Sandbox]:
    """A product, a monthly EUR price and a customer with a default test card, on the connected account."""
    kwargs = _account_kwargs()
    suffix = uuid.uuid4().hex[:8]
    product = stripe.Product.create(name=f"Revel #1426 canary {suffix}", **kwargs)
    price = stripe.Price.create(
        product=product.id, unit_amount=PRICE_CENTS, currency="eur", recurring={"interval": "month"}, **kwargs
    )
    customer = stripe.Customer.create(
        name=f"Revel #1426 canary {suffix}", email=f"revel-1426-{suffix}@example.com", **kwargs
    )
    payment_method = stripe.PaymentMethod.attach("pm_card_visa", customer=customer.id, **kwargs)
    stripe.Customer.modify(customer.id, invoice_settings={"default_payment_method": payment_method.id}, **kwargs)
    created = _Sandbox(product_id=product.id, price_id=price.id, customer_id=customer.id)
    try:
        yield created
    finally:
        # Cancelling subscriptions is the test's job (it knows their ids); deleting the customer
        # cancels any left behind.
        _safely("delete customer", lambda: stripe.Customer.delete(created.customer_id, **kwargs))
        _safely("deactivate price", lambda: stripe.Price.modify(created.price_id, active=False, **kwargs))
        _safely("archive product", lambda: stripe.Product.modify(created.product_id, active=False, **kwargs))


@pytest.fixture
def calendar_plan(organization: Organization, sandbox: _Sandbox) -> MembershipSubscriptionPlan:
    organization.stripe_account_id = settings.CONNECTED_TEST_STRIPE_ID
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(
        update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted", "updated_at"]
    )
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Calendar Monthly",
        price=Decimal("12.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
        period_alignment=MembershipSubscriptionPlan.PeriodAlignment.CALENDAR,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id=sandbox.product_id,
        stripe_price_id=sandbox.price_id,
    )


def test_checkout_accepts_the_calendar_anchor_params(
    calendar_plan: MembershipSubscriptionPlan, member_user: RevelUser, sandbox: _Sandbox
) -> None:
    """Stripe creates a subscription-mode Checkout Session with the helper's anchor params."""
    SiteSettings.get_solo()  # the success/cancel URLs derive from it
    organization = calendar_plan.tier.organization
    customer = CustomerProfile.objects.create(
        user=member_user, organization=organization, stripe_customer_id=sandbox.customer_id
    )
    subscription = MembershipSubscription.objects.create(
        user=member_user,
        plan=calendar_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )

    session = subscription_stripe_checkout._create_subscription_checkout_session(
        subscription, customer, idempotency_key=f"canary-1426:{uuid.uuid4().hex}"
    )
    try:
        assert session.url
        assert session.mode == "subscription"
    finally:
        _safely("expire checkout session", lambda: stripe.checkout.Session.expire(session.id, **_account_kwargs()))


def test_anchored_subscription_yields_the_documented_invoice_and_takes_the_fee(
    calendar_plan: MembershipSubscriptionPlan, member_user: RevelUser, sandbox: _Sandbox
) -> None:
    kwargs = _account_kwargs()
    now = timezone.now()
    boundary = first_period_end(calendar_plan, now)
    assert boundary is not None
    if is_free_stub(calendar_plan, now):
        pytest.skip("too close to a boundary: the stub is free by design, see the free-stub canaries")

    stripe_subscription = stripe.Subscription.create(
        customer=sandbox.customer_id,
        items=[{"price": sandbox.price_id}],
        billing_cycle_anchor=int(boundary.timestamp()),
        proration_behavior="create_prorations",
        application_fee_percent=FEE_PERCENT,
        expand=["latest_invoice"],
        **kwargs,
    )
    try:
        assert stripe_subscription.status == "active"
        item = stripe_subscription["items"]["data"][0]
        assert item["current_period_end"] == int(boundary.timestamp())

        invoice = stripe_subscription["latest_invoice"]
        assert invoice["billing_reason"] == "subscription_create"
        lines = invoice["lines"]["data"]
        assert len(lines) == 1
        line = lines[0]
        assert line["parent"]["subscription_item_details"]["proration"] is True
        assert line["pricing"]["price_details"]["price"] == sandbox.price_id
        assert line["period"]["end"] == int(boundary.timestamp())
        assert 0 < invoice["amount_paid"] < PRICE_CENTS  # a stub, not the full period

        # Replay what the webhooks would deliver through our sync.
        subscription = MembershipSubscription.objects.create(
            user=member_user,
            plan=calendar_plan,
            organization=calendar_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id=stripe_subscription.id,
        )
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)
        subscription_stripe_sync.sync_subscription_from_stripe(stripe_subscription)
        subscription.refresh_from_db()

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.platform_fee > 0, "the application fee must ride the prorated first charge"
        assert payment.full_period_amount == calendar_plan.price
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert subscription.current_period_end == boundary
        # The refund auto-cancel matches these two: the ledger row (invoice line) and the mirror (item).
        assert payment.period_start == subscription.current_period_start
        assert payment.period_end == subscription.current_period_end
    finally:
        _safely("cancel subscription", lambda: stripe.Subscription.cancel(stripe_subscription.id, **kwargs))


def _subscription_row(
    plan: MembershipSubscriptionPlan, user: RevelUser, stripe_subscription_id: str
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_subscription_id=stripe_subscription_id,
    )


def test_checkout_accepts_the_free_stub_params(
    calendar_plan: MembershipSubscriptionPlan, member_user: RevelUser, sandbox: _Sandbox
) -> None:
    """A stub below Stripe's minimum charge runs Checkout with ``proration_behavior="none"``."""
    SiteSettings.get_solo()
    organization = calendar_plan.tier.organization
    calendar_plan.price = Decimal("0.01")  # any stub of this is below the minimum: always a free stub
    calendar_plan.save(update_fields=["price"])
    customer = CustomerProfile.objects.create(
        user=member_user, organization=organization, stripe_customer_id=sandbox.customer_id
    )
    subscription = MembershipSubscription.objects.create(
        user=member_user,
        plan=calendar_plan,
        organization=organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )

    with mock.patch.object(stripe.checkout.Session, "create", wraps=stripe.checkout.Session.create) as create:
        session = subscription_stripe_checkout._create_subscription_checkout_session(
            subscription, customer, idempotency_key=f"canary-1426:{uuid.uuid4().hex}"
        )
    try:
        assert create.call_args.kwargs["subscription_data"]["proration_behavior"] == "none"
        assert session.url
        assert session.amount_total == 0
    finally:
        _safely("expire checkout session", lambda: stripe.checkout.Session.expire(session.id, **_account_kwargs()))


def test_free_stub_subscription_opens_no_charge_and_renews_on_the_boundary(
    calendar_plan: MembershipSubscriptionPlan, member_user: RevelUser, sandbox: _Sandbox
) -> None:
    kwargs = _account_kwargs()
    boundary = first_period_end(calendar_plan, timezone.now())
    assert boundary is not None

    stripe_subscription = stripe.Subscription.create(
        customer=sandbox.customer_id,
        items=[{"price": sandbox.price_id}],
        billing_cycle_anchor=int(boundary.timestamp()),
        proration_behavior="none",
        application_fee_percent=FEE_PERCENT,
        expand=["latest_invoice"],
        **kwargs,
    )
    try:
        assert stripe_subscription.status == "active"
        assert stripe_subscription["items"]["data"][0]["current_period_end"] == int(boundary.timestamp())
        invoice = stripe_subscription.get("latest_invoice")
        assert (
            invoice is None or invoice["amount_paid"] == 0
        )  # Subscription.create opens none; Checkout may open a $0 one

        subscription = _subscription_row(calendar_plan, member_user, stripe_subscription.id)
        if invoice is not None:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)
            assert payment is not None
            assert payment.amount == Decimal("0")
        subscription_stripe_sync.sync_subscription_from_stripe(stripe_subscription)
        subscription.refresh_from_db()
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert subscription.current_period_end == boundary
    finally:
        _safely("cancel subscription", lambda: stripe.Subscription.cancel(stripe_subscription.id, **kwargs))


def test_below_minimum_prorated_stub_is_the_documented_zero_paid_shape(
    calendar_plan: MembershipSubscriptionPlan, member_user: RevelUser, sandbox: _Sandbox
) -> None:
    """A prorated stub too small to charge settles at ``amount_paid`` 0 with no PaymentIntent."""
    kwargs = _account_kwargs()
    anchor = timezone.now() + datetime.timedelta(minutes=45)

    stripe_subscription = stripe.Subscription.create(
        customer=sandbox.customer_id,
        items=[{"price": sandbox.price_id}],
        billing_cycle_anchor=int(anchor.timestamp()),
        proration_behavior="create_prorations",
        application_fee_percent=FEE_PERCENT,
        expand=["latest_invoice.lines", "latest_invoice.payments"],
        **kwargs,
    )
    try:
        invoice = stripe_subscription["latest_invoice"]
        assert invoice["billing_reason"] == "subscription_create"
        assert invoice["amount_paid"] == 0
        assert invoice["payments"]["data"] == []

        subscription = _subscription_row(calendar_plan, member_user, stripe_subscription.id)
        payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)
        subscription_stripe_sync.sync_subscription_from_stripe(stripe_subscription)
        subscription.refresh_from_db()

        assert payment is not None
        assert payment.amount == Decimal("0")
        assert payment.stripe_payment_intent_id == ""
        assert payment.full_period_amount == calendar_plan.price  # read off the line's real Stripe Price
        assert subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE
        assert payment.period_start == subscription.current_period_start
    finally:
        _safely("cancel subscription", lambda: stripe.Subscription.cancel(stripe_subscription.id, **kwargs))
