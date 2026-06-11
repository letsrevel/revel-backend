"""Integration tests against the real Stripe sandbox (test mode).

These hit the live Stripe test-mode API and are excluded from normal test
runs and CI (see ``addopts`` in pyproject.toml). They verify the Phase 2
(#483) behaviours end-to-end:

- outbound calls succeed with ``stripe.api_version`` pinned to
  ``settings.STRIPE_API_VERSION`` (a bad pin would 400 on every call);
- objects rendered at the pinned version carry no embedded ``refunds`` list
  on Charge — the premise behind ``_resolve_refunds``;
- a pinned-shape ``charge.refunded`` event (no embedded refunds) is fully
  processed by fetching the refunds outbound via ``stripe.Refund.list``;
- ``create_checkout_session`` works against the connected test account.

Run manually with:
    pytest -m integration src/events/tests/test_service/test_stripe_integration.py -v

Requires real test-mode keys in .env (``STRIPE_SECRET_KEY=sk_test_…``); the
checkout-session test additionally needs ``CONNECTED_TEST_STRIPE_ID``.
"""

import typing as t
import uuid

import pytest
import stripe
from django.conf import settings

from events.models import Event, Payment, StripeWebhookEvent, Ticket, TicketTier
from events.service import stripe_service, stripe_webhooks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.django_db,
    pytest.mark.skipif(
        settings.STRIPE_SECRET_KEY in {"sk_test_...", ""},
        reason="needs real Stripe test-mode keys in .env",
    ),
]


def _create_refunded_intent(amount_cents: int) -> tuple[stripe.PaymentIntent, stripe.Refund]:
    """Create a confirmed test-mode PaymentIntent and refund it in full."""
    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="eur",
        payment_method="pm_card_visa",
        confirm=True,
        payment_method_types=["card"],
    )
    assert intent.status == "succeeded", f"test intent did not confirm: {intent.status}"
    refund = stripe.Refund.create(payment_intent=intent.id)
    return intent, refund


class TestPinnedApiVersion:
    def test_pin_accepted_and_charge_renders_without_embedded_refunds(self) -> None:
        """The pinned version is accepted outbound; Charge has no refunds list.

        The second assertion is the empirical premise behind
        ``_resolve_refunds``: at API versions >= 2022-11-15 (so at the pinned
        version too) Stripe no longer embeds the ``refunds`` list on Charge —
        neither in retrieves nor in webhook payloads rendered at that version.
        """
        intent, _refund = _create_refunded_intent(1000)
        charge = stripe.Charge.retrieve(t.cast(str, intent.latest_charge))
        assert charge.refunded is True
        assert "refunds" not in charge


class TestChargeRefundedOutboundFetch:
    def test_pinned_shape_payload_processed_via_refund_list(
        self,
        ticket: Ticket,
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A charge.refunded payload without embedded refunds is fully handled.

        The refunds come from a real ``stripe.Refund.list`` call against the
        sandbox; matching, payment/ticket mutation and the event log row all
        behave as in production.
        """
        intent, refund = _create_refunded_intent(4000)  # == payment_factory's 40.00 EUR
        payment = payment_factory(
            ticket,
            stripe_payment_intent_id=intent.id,
            stripe_session_id=f"cs_integration_{uuid.uuid4().hex}",
        )

        event = stripe.Event.construct_from(
            {
                "id": f"evt_integration_{uuid.uuid4().hex}",
                "object": "event",
                "type": "charge.refunded",
                "livemode": False,
                # Pinned-version shape: no "refunds" key on the charge at all.
                "data": {
                    "object": {
                        "id": intent.latest_charge,
                        "object": "charge",
                        "payment_intent": intent.id,
                    }
                },
            },
            stripe.api_key,
        )
        stripe_webhooks.handle_event(event)

        payment.refresh_from_db()
        assert payment.refund_status == Payment.RefundStatus.SUCCEEDED
        assert payment.status == Payment.PaymentStatus.REFUNDED
        assert payment.stripe_refund_id == refund.id
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED
        row = StripeWebhookEvent.objects.get(event_id=event.id)
        assert row.outcome == StripeWebhookEvent.Outcome.HANDLED


@pytest.mark.skipif(
    not settings.CONNECTED_TEST_STRIPE_ID,
    reason="needs CONNECTED_TEST_STRIPE_ID (a real connected test account) in .env",
)
class TestCheckoutSessionAtPinnedVersion:
    def test_create_checkout_session_succeeds(
        self,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: t.Any,
    ) -> None:
        """The full service-layer Session.create works at the pinned version.

        Exercises line_items/price_data, payment_intent_data with an
        application fee, metadata, expires_at and the Stripe-Account header —
        the heaviest outbound call we make.
        """
        org = event.organization
        org.stripe_account_id = settings.CONNECTED_TEST_STRIPE_ID
        org.stripe_charges_enabled = True
        org.stripe_details_submitted = True
        org.save(
            update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted", "updated_at"]
        )

        url, payment = stripe_service.create_checkout_session(event, event_ticket_tier, member_user)

        assert url.startswith("https://checkout.stripe.com/")
        assert payment.status == Payment.PaymentStatus.PENDING
        assert payment.stripe_session_id.startswith("cs_test_")
