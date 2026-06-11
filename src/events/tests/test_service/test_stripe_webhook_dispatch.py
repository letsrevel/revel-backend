"""Tests for multi-secret webhook verification and idempotent event dispatch."""

import json
import time
import typing as t
from unittest.mock import patch

import pytest
import stripe
from django.db import transaction
from django.test import override_settings

from events.exceptions import InvalidStripeWebhookSignatureError
from events.models import Payment, StripeWebhookEvent, Ticket
from events.service import stripe_webhooks

pytestmark = pytest.mark.django_db


def _signed_payload(event_dict: dict[str, t.Any], secret: str) -> tuple[bytes, str]:
    """Serialize *event_dict* and produce a valid Stripe-Signature header for *secret*."""
    payload = json.dumps(event_dict).encode()
    timestamp = int(time.time())
    signature = stripe.WebhookSignature._compute_signature(  # noqa: SLF001
        f"{timestamp}.{payload.decode()}", secret
    )
    return payload, f"t={timestamp},v1={signature}"


_EVENT: dict[str, t.Any] = {
    "id": "evt_test_1",
    "object": "event",
    "type": "payment_intent.canceled",
    "livemode": False,
    "data": {"object": {"id": "pi_none", "object": "payment_intent"}},
}


@override_settings(STRIPE_WEBHOOK_SECRETS=["whsec_first"])
def test_single_secret_verifies() -> None:
    """The legacy single-secret setup keeps verifying."""
    payload, header = _signed_payload(_EVENT, "whsec_first")
    event = stripe_webhooks.verify_webhook(payload, header)
    assert event.id == "evt_test_1"


@override_settings(STRIPE_WEBHOOK_SECRETS=["whsec_platform", "whsec_connect"])
def test_second_secret_verifies_when_first_does_not_match() -> None:
    """Two-endpoint setup: a Connect-signed delivery must verify via the second secret."""
    payload, header = _signed_payload(_EVENT, "whsec_connect")
    event = stripe_webhooks.verify_webhook(payload, header)
    assert event.id == "evt_test_1"


@override_settings(STRIPE_WEBHOOK_SECRETS=["whsec_a", "whsec_b"])
def test_no_secret_matches_raises() -> None:
    """A delivery signed with an unknown secret fails closed."""
    payload, header = _signed_payload(_EVENT, "whsec_other")
    with pytest.raises(InvalidStripeWebhookSignatureError):
        stripe_webhooks.verify_webhook(payload, header)


@override_settings(STRIPE_WEBHOOK_SECRETS=["whsec_..."])
def test_placeholder_only_fails_closed() -> None:
    """The settings-default placeholder never verifies anything."""
    payload, header = _signed_payload(_EVENT, "whsec_...")
    with pytest.raises(InvalidStripeWebhookSignatureError):
        stripe_webhooks.verify_webhook(payload, header)


@override_settings(STRIPE_WEBHOOK_SECRETS=["whsec_first"])
def test_malformed_json_after_valid_signature_is_terminal() -> None:
    """A matching HMAC over a non-JSON body is a malformed payload, not a signature miss."""
    payload = b"not-json"
    timestamp = int(time.time())
    signature = stripe.WebhookSignature._compute_signature(  # noqa: SLF001
        f"{timestamp}.not-json", "whsec_first"
    )
    header = f"t={timestamp},v1={signature}"
    with pytest.raises(InvalidStripeWebhookSignatureError):
        stripe_webhooks.verify_webhook(payload, header)


def _make_event(event_id: str = "evt_dedup_1", event_type: str = "payment_intent.canceled") -> stripe.Event:
    return stripe.Event.construct_from(
        {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "livemode": False,
            "account": "acct_conn_1",
            "data": {"object": {"id": "pi_none", "object": "payment_intent"}},
        },
        "sk_test_x",
    )


def test_handle_event_records_and_marks_handled() -> None:
    """First delivery inserts a log row and marks it HANDLED for a mapped type."""
    stripe_webhooks.handle_event(_make_event())
    row = StripeWebhookEvent.objects.get(event_id="evt_dedup_1")
    assert row.outcome == StripeWebhookEvent.Outcome.HANDLED
    assert row.event_type == "payment_intent.canceled"
    assert row.account == "acct_conn_1"
    assert row.payload["id"] == "evt_dedup_1"


def test_handle_event_unknown_type_marked_unhandled() -> None:
    """An unmapped event type is logged as UNHANDLED, not an error."""
    stripe_webhooks.handle_event(_make_event(event_id="evt_unknown", event_type="customer.created"))
    row = StripeWebhookEvent.objects.get(event_id="evt_unknown")
    assert row.outcome == StripeWebhookEvent.Outcome.UNHANDLED


def test_handle_event_duplicate_is_noop() -> None:
    """A redelivered event id must not reach the full handler a second time."""
    stripe_webhooks.handle_event(_make_event(event_id="evt_dup"))
    with patch.object(stripe_webhooks.StripeEventHandler, "handle") as spy:
        stripe_webhooks.handle_event(_make_event(event_id="evt_dup"))
    spy.assert_not_called()
    assert StripeWebhookEvent.objects.filter(event_id="evt_dup").count() == 1


def _make_checkout_event(session_id: str, event_id: str = "evt_checkout_replay") -> stripe.Event:
    return stripe.Event.construct_from(
        {
            "id": event_id,
            "object": "event",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "id": session_id,
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "payment_intent": "pi_replay_checkout",
                }
            },
        },
        "sk_test_x",
    )


def _make_refund_event(payment_intent_id: str, event_id: str = "evt_refund_replay") -> stripe.Event:
    return stripe.Event.construct_from(
        {
            "id": event_id,
            "object": "event",
            "type": "charge.refunded",
            "livemode": False,
            "data": {
                "object": {
                    "id": "ch_replay",
                    "object": "charge",
                    "payment_intent": payment_intent_id,
                    "refunds": {"data": []},
                }
            },
        },
        "sk_test_x",
    )


def test_duplicate_checkout_replay_reenqueues_invoice(
    ticket: Ticket,
    payment_factory: t.Callable[..., Payment],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A redelivered checkout completion retries the invoice dispatch.

    The first delivery's on_commit ``.delay()`` runs after the dedup row has
    committed and may fail (e.g. broker outage); the redelivery must
    re-enqueue the idempotent invoice task instead of pure no-oping.
    """
    payment_factory(ticket, stripe_session_id="cs_replay_1")
    stripe_webhooks.handle_event(_make_checkout_event("cs_replay_1"))
    with (
        patch("events.tasks.generate_attendee_invoice_task.delay") as delay_spy,
        django_capture_on_commit_callbacks(execute=True),
    ):
        stripe_webhooks.handle_event(_make_checkout_event("cs_replay_1"))
    delay_spy.assert_called_once_with("cs_replay_1")


def test_duplicate_checkout_replay_skips_unknown_session(
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Replaying a checkout completion with no matching payments enqueues nothing."""
    StripeWebhookEvent.objects.create(event_id="evt_checkout_orphan", event_type="checkout.session.completed")
    with (
        patch("events.tasks.generate_attendee_invoice_task.delay") as delay_spy,
        django_capture_on_commit_callbacks(execute=True),
    ):
        stripe_webhooks.handle_event(_make_checkout_event("cs_orphan", event_id="evt_checkout_orphan"))
    delay_spy.assert_not_called()


def test_duplicate_refund_replay_reenqueues_credit_note(
    ticket: Ticket,
    payment_factory: t.Callable[..., Payment],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A redelivered charge.refunded retries the credit-note dispatch for refunded payments."""
    payment = payment_factory(
        ticket,
        stripe_session_id="cs_replay_refund",
        stripe_payment_intent_id="pi_replay_refund",
        refund_status=Payment.RefundStatus.SUCCEEDED,
    )
    StripeWebhookEvent.objects.create(event_id="evt_refund_replay", event_type="charge.refunded")
    with (
        patch("events.tasks.generate_attendee_credit_note_task.delay") as delay_spy,
        django_capture_on_commit_callbacks(execute=True),
    ):
        stripe_webhooks.handle_event(_make_refund_event("pi_replay_refund"))
    delay_spy.assert_called_once_with("cs_replay_refund", [str(payment.id)])


def test_handler_error_rolls_back_dedup_row() -> None:
    """If the handler raises, the log row rolls back so a Stripe retry reprocesses."""
    with (
        patch.object(stripe_webhooks.StripeEventHandler, "handle", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        stripe_webhooks.handle_event(_make_event(event_id="evt_err"))
    assert not StripeWebhookEvent.objects.filter(event_id="evt_err").exists()
