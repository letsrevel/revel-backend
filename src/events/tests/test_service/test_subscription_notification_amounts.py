"""Regressions for what subscription notifications quote — and to whom.

Two fixes are covered here:

* **Amounts** — renewal receipts, failure warnings and renewal reminders used
  to quote ``plan.price``. A plan price change mints a NEW Stripe Price and
  leaves existing subscribers on the old one (grandfathering), so the figure
  members were shown was routinely one they had never been charged.
* **Revived subscribers** — a revival's first ``invoice.paid`` arrives with
  ``prior_status=PENDING``, which the renewal gate rejected, while
  MEMBERSHIP_GRANTED never fired either (the member's row is *updated*, not
  created). The member paid and heard nothing at all.
"""

import typing as t
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)
from events.service import subscription_stripe_sync
from events.tasks import send_subscription_renewal_reminders
from events.utils.subscription_periods import REMINDER_DAYS
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_amounts_test"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    return organization


@pytest.fixture
def tier(stripe_org: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=stripe_org, name="AmountsTier")


@pytest.fixture
def online_plan(tier: MembershipTier) -> MembershipSubscriptionPlan:
    """Plan whose list price was raised to 20 — existing members stay at 10."""
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("20.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_amounts",
        stripe_price_id="price_amounts",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="amounts_subscriber", email="amounts@example.com", password="pass"
    )


def _make_sub(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    *,
    stripe_id: str,
    status: str = MembershipSubscription.SubscriptionStatus.ACTIVE,
) -> MembershipSubscription:
    now = timezone.now()
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=plan.tier.organization,
        status=status,
        stripe_subscription_id=stripe_id,
        current_period_start=now - timedelta(days=15),
        current_period_end=now + timedelta(days=15),
    )


def _invoice(
    stripe_sub_id: str,
    *,
    invoice_id: str,
    amount_paid: int = 0,
    amount_due: int = 0,
) -> dict[str, t.Any]:
    now_epoch = int(timezone.now().timestamp())
    return {
        "id": invoice_id,
        "subscription": stripe_sub_id,
        "amount_paid": amount_paid,
        "amount_due": amount_due,
        "currency": "eur",
        "payment_intent": "pi_amounts_test",
        "billing_reason": "subscription_cycle",
        "lines": {"data": [{"period": {"start": now_epoch - 86400, "end": now_epoch + 30 * 86400}}]},
    }


def _notifications(user: RevelUser, nt: NotificationType) -> list[Notification]:
    return list(Notification.objects.filter(user=user, notification_type=nt))


def _record_paid(invoice: dict[str, t.Any], capture_on_commit: t.Any) -> None:
    """Handle ``invoice.paid`` with on_commit callbacks actually executed.

    The MEMBERSHIP_GRANTED signal defers to ``transaction.on_commit``, which
    pytest-django never reaches; delivery itself is mocked out so executing the
    callbacks only creates the notification rows.
    """
    with mock.patch("notifications.tasks.dispatch_notification.delay"), capture_on_commit(execute=True):
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)


class TestInvoiceNotificationAmounts:
    def test_renewal_receipt_quotes_amount_paid_not_plan_price(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """A grandfathered member charged 10.00 must not be told they paid 20.00."""
        _make_sub(online_plan, subscriber, stripe_id="sub_amt1")
        invoice = _invoice("sub_amt1", invoice_id="in_amt1", amount_paid=1000, amount_due=1000)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        notifs = _notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)
        assert len(notifs) == 1
        assert notifs[0].context["amount"] == "10.00 EUR"

    def test_payment_failed_quotes_amount_due_not_plan_price(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """The failure warning quotes what the invoice asked for."""
        _make_sub(online_plan, subscriber, stripe_id="sub_amt2")
        invoice = _invoice("sub_amt2", invoice_id="in_amt2", amount_due=1000)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)

        notifs = _notifications(subscriber, NotificationType.SUBSCRIPTION_PAYMENT_FAILED)
        assert len(notifs) == 1
        assert notifs[0].context["amount"] == "10.00 EUR"

    def test_missing_amount_falls_back_to_plan_price(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Payloads without a usable amount keep the pre-fix behaviour."""
        _make_sub(online_plan, subscriber, stripe_id="sub_amt3")
        invoice = _invoice("sub_amt3", invoice_id="in_amt3")
        invoice.pop("amount_due")

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)

        notifs = _notifications(subscriber, NotificationType.SUBSCRIPTION_PAYMENT_FAILED)
        assert len(notifs) == 1
        assert notifs[0].context["amount"] == "20.00 EUR"


class TestRevivalConfirmation:
    def test_revival_first_invoice_paid_confirms_exactly_once(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A revived member must hear that their payment went through.

        Regression: ``create_revival_checkout`` puts the member's existing row
        back to PENDING, so the first ``invoice.paid`` failed the renewal gate
        (which required ACTIVE/PAST_DUE); MEMBERSHIP_GRANTED did not cover it
        either, because the OrganizationMember row already exists and is only
        updated. Net effect: the member was charged in silence.
        """
        sub = _make_sub(
            online_plan,
            subscriber,
            stripe_id="sub_revive",
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        # The previous life's ledger is what survives a revival.
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=60),
            period_end=timezone.now() - timedelta(days=30),
            stripe_invoice_id="in_previous_life",
        )
        # ... as does the member row, CANCELLED when the subscription lapsed.
        OrganizationMember.objects.create(
            organization=online_plan.tier.organization,
            user=subscriber,
            tier=online_plan.tier,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        Notification.objects.all().delete()  # ignore anything the setup emitted

        invoice = _invoice("sub_revive", invoice_id="in_revive", amount_paid=1000, amount_due=1000)
        # MEMBERSHIP_GRANTED is dispatched from an on_commit callback, so it only
        # materializes when the callbacks are executed — capture them, or the
        # "no MEMBERSHIP_GRANTED here" assertion below would be vacuous.
        _record_paid(invoice, django_capture_on_commit_callbacks)

        notifs = _notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)
        assert len(notifs) == 1
        assert notifs[0].context["amount"] == "10.00 EUR"
        assert not _notifications(subscriber, NotificationType.MEMBERSHIP_GRANTED)

    def test_revival_invoice_redelivery_does_not_double_notify(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Stripe re-delivering the revival invoice must not re-confirm."""
        sub = _make_sub(
            online_plan,
            subscriber,
            stripe_id="sub_revive2",
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=60),
            period_end=timezone.now() - timedelta(days=30),
            stripe_invoice_id="in_previous_life2",
        )
        invoice = _invoice("sub_revive2", invoice_id="in_revive2", amount_paid=1000, amount_due=1000)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)
        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert len(_notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)) == 1

    def test_first_ever_subscription_still_only_gets_membership_granted(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A first purchase has no payment history, so the revival gate stays shut."""
        _make_sub(
            online_plan,
            subscriber,
            stripe_id="sub_first",
            status=MembershipSubscription.SubscriptionStatus.PENDING,
        )
        invoice = _invoice("sub_first", invoice_id="in_first", amount_paid=2000, amount_due=2000)

        _record_paid(invoice, django_capture_on_commit_callbacks)

        assert not _notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)
        # The member row was created by this payment, so MEMBERSHIP_GRANTED covers it.
        assert OrganizationMember.objects.filter(organization=online_plan.tier.organization, user=subscriber).exists()
        assert len(_notifications(subscriber, NotificationType.MEMBERSHIP_GRANTED)) == 1

    def test_ordinary_renewal_unchanged(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """An ACTIVE → ACTIVE renewal still fires exactly one receipt."""
        _make_sub(online_plan, subscriber, stripe_id="sub_ordinary")
        invoice = _invoice("sub_ordinary", invoice_id="in_ordinary", amount_paid=2000, amount_due=2000)

        subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert len(_notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED)) == 1


class TestRenewalReminderAmount:
    def test_reminder_quotes_last_real_payment_not_plan_price(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """No invoice exists yet, so the last real payment is the honest estimate."""
        period_end_day = timezone.localdate() + timedelta(days=REMINDER_DAYS)
        sub = MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=timezone.make_aware(datetime.combine(period_end_day, time(12, 0))),
        )
        MembershipPayment.objects.create(
            subscription=sub,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now() - timedelta(days=30),
            period_end=timezone.now(),
            raw_response={"billing_reason": "subscription_cycle"},
        )

        assert send_subscription_renewal_reminders()["sent"] == 1

        notifs = _notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_REMINDER)
        assert len(notifs) == 1
        assert notifs[0].context["amount"] == "10.00 EUR"

    def test_reminder_falls_back_to_plan_price_without_payments(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        period_end_day = timezone.localdate() + timedelta(days=REMINDER_DAYS)
        MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=timezone.make_aware(datetime.combine(period_end_day, time(12, 0))),
        )

        assert send_subscription_renewal_reminders()["sent"] == 1

        notifs = _notifications(subscriber, NotificationType.SUBSCRIPTION_RENEWAL_REMINDER)
        assert len(notifs) == 1
        assert notifs[0].context["amount"] == "20.00 EUR"
