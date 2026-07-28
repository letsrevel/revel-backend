"""Tests for the ``checkout.session.completed`` → subscription link routing.

Hosted-Checkout subscriptions (mode=subscription) have no Payment/Ticket rows;
the handler links ``session.subscription`` onto the local PENDING row via
``metadata.membership_subscription_id`` without disturbing the ticket path.
"""

import typing as t
from decimal import Decimal
from unittest import mock
from unittest.mock import MagicMock

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


def _session_event(session: dict[str, t.Any], *, event_type: str = "checkout.session.completed") -> MagicMock:
    """Build a mock checkout.session.* event around ``session``."""
    event_data = {"id": "evt_sub_checkout", "type": event_type, "data": {"object": session}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = session
    return mock_event


@pytest.fixture
def online_plan(organization: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_test",
        stripe_price_id="price_test",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="checkout_subscriber", email="checkout-sub@example.com", password="pass"
    )


@pytest.fixture
def pending_subscription(
    online_plan: MembershipSubscriptionPlan,
    subscriber: RevelUser,
) -> MembershipSubscription:
    return MembershipSubscription.objects.create(
        user=subscriber,
        plan=online_plan,
        organization=online_plan.tier.organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
        stripe_checkout_session_id="cs_sub_test",
    )


def _paid_invoice_payload(stripe_sub_id: str, *, invoice_id: str) -> dict[str, t.Any]:
    import time

    now_epoch = int(time.time())
    return {
        "id": invoice_id,
        "status": "paid",
        "subscription": stripe_sub_id,
        "amount_paid": 1000,
        "currency": "eur",
        "payment_intent": "pi_backfill",
        "lines": {"data": [{"period": {"start": now_epoch - 86400, "end": now_epoch + 30 * 86400}}]},
    }


class TestInitialInvoiceBackfill:
    """Out-of-order first ``invoice.paid`` recovery via checkout completion.

    Stripe guarantees no ordering: if ``invoice.paid`` lands before
    ``checkout.session.completed`` links ``stripe_subscription_id``, the
    invoice event is silently dropped. The link handler must replay the
    already-paid initial invoice so the ledger row and activation are never
    lost.
    """

    def test_backfills_paid_initial_invoice(self, pending_subscription: MembershipSubscription) -> None:
        from events.models import MembershipPayment

        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_backfill",
            "invoice": "in_backfill",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                stripe.Invoice,
                "retrieve",
                classmethod(lambda cls, *a, **kw: _paid_invoice_payload("sub_backfill", invoice_id="in_backfill")),
            )
            StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_backfill"
        payment = MembershipPayment.objects.get(subscription=pending_subscription)
        assert payment.stripe_invoice_id == "in_backfill"
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        # Activation no longer depends on the (dropped) invoice.paid event.
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE

    def test_backfill_skips_unpaid_invoice(self, pending_subscription: MembershipSubscription) -> None:
        from events.models import MembershipPayment

        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_backfill_open",
            "invoice": "in_backfill_open",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        open_invoice = _paid_invoice_payload("sub_backfill_open", invoice_id="in_backfill_open")
        open_invoice["status"] = "open"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(stripe.Invoice, "retrieve", classmethod(lambda cls, *a, **kw: open_invoice))
            StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert not MembershipPayment.objects.filter(subscription=pending_subscription).exists()
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_backfill_skips_when_payment_already_recorded(self, pending_subscription: MembershipSubscription) -> None:
        """Normal ordering (invoice.paid already processed): no Stripe retrieve at all."""
        from events.models import MembershipPayment

        MembershipPayment.objects.create(
            subscription=pending_subscription,
            amount=Decimal("10.00"),
            currency="EUR",
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
            period_start=timezone.now(),
            period_end=timezone.now(),
            stripe_invoice_id="in_already",
        )
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_already",
            "invoice": "in_already",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }

        def _boom(*a: t.Any, **kw: t.Any) -> t.NoReturn:
            raise AssertionError("Invoice.retrieve must not be called when a payment exists")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(stripe.Invoice, "retrieve", _boom)
            StripeEventHandler(_session_event(session)).handle()

        assert MembershipPayment.objects.filter(subscription=pending_subscription).count() == 1


class TestRedeliveredCheckoutSelfHeal:
    """``stripe_webhooks.replay`` routes redelivered subscription checkouts back here.

    The whole point is re-running the idempotent initial-invoice backfill, so
    the already-linked branch must not short-circuit past it.
    """

    def test_redelivery_backfills_invoice_that_was_still_open_first_time(
        self,
        pending_subscription: MembershipSubscription,
    ) -> None:
        from events.models import MembershipPayment

        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_replay",
            "invoice": "in_replay",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        open_invoice = _paid_invoice_payload("sub_replay", invoice_id="in_replay")
        open_invoice["status"] = "open"
        # First delivery: the invoice hasn't settled yet, so nothing is recorded.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(stripe.Invoice, "retrieve", classmethod(lambda cls, *a, **kw: open_invoice))
            StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_replay"
        assert not MembershipPayment.objects.filter(subscription=pending_subscription).exists()

        # Its ``invoice.paid`` never arrived. Stripe's redelivery of the checkout
        # event is the self-heal — the row is already linked, but the backfill
        # must still run.
        paid_invoice = _paid_invoice_payload("sub_replay", invoice_id="in_replay")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(stripe.Invoice, "retrieve", classmethod(lambda cls, *a, **kw: paid_invoice))
            StripeEventHandler(_session_event(session)).handle()
            # A further redelivery must not duplicate the ledger row.
            StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert MembershipPayment.objects.filter(subscription=pending_subscription).count() == 1
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE


class TestCheckoutCompletedAgainstTerminalRow:
    """A session paid after the row went terminal must not resurrect it."""

    def _terminalize(self, subscription: MembershipSubscription) -> None:
        subscription.status = MembershipSubscription.SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.save(update_fields=["status", "cancelled_at", "updated_at"])

    def test_does_not_link_and_cancels_the_stripe_subscription(
        self,
        pending_subscription: MembershipSubscription,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        from events.models import MembershipPayment

        self._terminalize(pending_subscription)
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_after_cancel",
            "invoice": "in_after_cancel",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        with (
            mock.patch(
                "events.service.subscription_stripe_service.cancel_stripe_subscription_best_effort"
            ) as mock_cancel,
            mock.patch("events.service.stripe_incidents.record_subscription_checkout_while_terminal") as mock_incident,
            django_capture_on_commit_callbacks(execute=True),
        ):
            StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        # Frozen: no link, no ledger row, no membership.
        assert pending_subscription.stripe_subscription_id is None
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.CANCELLED
        assert not MembershipPayment.objects.filter(subscription=pending_subscription).exists()
        # ...but the orphan Stripe subscription is closed and an operator is told.
        mock_incident.assert_called_once()
        assert mock_incident.call_args.kwargs["stripe_subscription_id"] == "sub_after_cancel"
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.args[0].stripe_subscription_id == "sub_after_cancel"

    def test_does_not_hit_stripe_under_the_row_lock(
        self,
        pending_subscription: MembershipSubscription,
    ) -> None:
        """The cancel is deferred to ``on_commit`` — nothing fires inside the handler."""
        self._terminalize(pending_subscription)
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_after_cancel",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        with mock.patch(
            "events.service.subscription_stripe_service.cancel_stripe_subscription_best_effort"
        ) as mock_cancel:
            StripeEventHandler(_session_event(session)).handle()

        mock_cancel.assert_not_called()


class TestSubscriptionCheckoutCompleted:
    def test_links_stripe_subscription_id(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_from_session",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        handler = StripeEventHandler(_session_event(session))

        assert handler.handle() is True

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_from_session"
        # Status stays PENDING — invoice.paid flips it to ACTIVE downstream.
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_links_expanded_subscription_object(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": {"id": "sub_expanded"},
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_expanded"

    def test_redelivery_is_idempotent(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_once",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_once"

    def test_subscription_mode_without_metadata_is_ignored(self, pending_subscription: MembershipSubscription) -> None:
        """Orgs can run their own subscription checkouts on the same Connect account."""
        session = {
            "id": "cs_foreign",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_foreign",
            "metadata": {},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id is None

    def test_unknown_local_row_is_tolerated(self) -> None:
        session = {
            "id": "cs_orphan",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_orphan",
            "metadata": {"membership_subscription_id": "00000000-0000-0000-0000-000000000000"},
        }
        # Must not raise — a 500 would wedge Stripe redelivery.
        StripeEventHandler(_session_event(session)).handle()

    def test_malformed_metadata_is_tolerated(self) -> None:
        session = {
            "id": "cs_bad_meta",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": "sub_bad",
            "metadata": {"membership_subscription_id": "not-a-uuid"},
        }
        StripeEventHandler(_session_event(session)).handle()

    def test_missing_subscription_reference_is_tolerated(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "mode": "subscription",
            "payment_status": "paid",
            "subscription": None,
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id is None

    def test_metadata_only_routing_without_mode(self, pending_subscription: MembershipSubscription) -> None:
        """A session carrying our metadata key routes to the subscription handler even without mode."""
        session = {
            "id": "cs_sub_test",
            "payment_status": "paid",
            "subscription": "sub_meta_routed",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(_session_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.stripe_subscription_id == "sub_meta_routed"


class TestSubscriptionCheckoutExpired:
    """``checkout.session.expired`` frees the cap slot an abandoned checkout holds."""

    def _expired_event(self, session: dict[str, t.Any]) -> MagicMock:
        return _session_event(session, event_type="checkout.session.expired")

    def test_clears_abandoned_pending_row(self, pending_subscription: MembershipSubscription) -> None:
        session = {
            "id": "cs_sub_test",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        handler = StripeEventHandler(self._expired_event(session))

        assert handler.handle() is True
        assert not MembershipSubscription.objects.filter(pk=pending_subscription.pk).exists()

    def test_revival_row_reverts_to_expired(self, pending_subscription: MembershipSubscription) -> None:
        """A row with ledger history is reverted to EXPIRED, keeping the revival window."""
        from events.models import MembershipPayment

        now = timezone.now()
        MembershipPayment.objects.create(
            subscription=pending_subscription,
            amount=Decimal("10.00"),
            currency="EUR",
            period_start=now,
            period_end=now,
        )
        session = {
            "id": "cs_sub_test",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(self._expired_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.EXPIRED
        assert pending_subscription.stripe_checkout_session_id == ""

    def test_completed_row_is_left_alone(self, pending_subscription: MembershipSubscription) -> None:
        """Out-of-order delivery: a linked row means the session completed — never clear it."""
        pending_subscription.stripe_subscription_id = "sub_linked"
        pending_subscription.save(update_fields=["stripe_subscription_id", "updated_at"])
        session = {
            "id": "cs_sub_test",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(self._expired_event(session)).handle()

        pending_subscription.refresh_from_db()
        assert pending_subscription.status == MembershipSubscription.SubscriptionStatus.PENDING

    def test_superseded_session_is_ignored(self, pending_subscription: MembershipSubscription) -> None:
        """An expiry for an OLD session must not clear a row already on a newer one."""
        session = {
            "id": "cs_older_session",
            "metadata": {"membership_subscription_id": str(pending_subscription.pk)},
        }
        StripeEventHandler(self._expired_event(session)).handle()

        assert MembershipSubscription.objects.filter(pk=pending_subscription.pk).exists()

    def test_sessions_without_metadata_are_ignored(self, pending_subscription: MembershipSubscription) -> None:
        """Ticket checkouts / org-run sessions carry no membership metadata."""
        StripeEventHandler(self._expired_event({"id": "cs_ticket", "metadata": {}})).handle()

        assert MembershipSubscription.objects.filter(pk=pending_subscription.pk).exists()
