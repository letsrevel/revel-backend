"""Tests for the staff notification raised when a refund can't be auto-matched.

The matcher refuses to guess which ticket a Stripe-Dashboard refund belongs to
when the intent's Payments differ in amount (non-uniform) or no interpretation
fits (ambiguous). Money moved in Stripe and nothing changed in Revel, so the
refusal must leave a durable, operator-visible record — issue #741.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
import stripe

from common.models import SiteSettings
from events.models import Event, Payment, Ticket, TicketTier
from events.models.venue import VenueSeat
from events.service.stripe_webhooks import StripeEventHandler, handle_event
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db


def _charge_event(
    payment_intent_id: str, refunds: list[dict[str, t.Any]], event_id: str = "evt_refund"
) -> stripe.Event:
    ev = MagicMock(spec=stripe.Event)
    ev.id = event_id
    ev.type = "charge.refunded"
    ev.account = None
    ev.livemode = False
    ev.data = MagicMock()
    ev.data.object = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
        "refunds": {"data": refunds},
    }
    ev.__iter__.return_value = iter([])
    return ev


def _batch(payments: list[Payment], intent_id: str) -> None:
    Payment.objects.filter(pk__in=[p.pk for p in payments]).update(
        stripe_payment_intent_id=intent_id, status=Payment.PaymentStatus.SUCCEEDED
    )


def _unmatched_notifications() -> list[Notification]:
    return list(Notification.objects.filter(notification_type=NotificationType.REFUND_UNMATCHED))


@pytest.fixture
def mixed_price_batch(
    payment_factory: t.Callable[..., Payment],
    ticket_factory: t.Callable[..., Ticket],
    tier_online_with_cancellation_enabled: TicketTier,
    seated_event: tuple[Event, list[VenueSeat]],
) -> list[Payment]:
    """Two seats on one intent at different prices — the shape #739 made unmatchable."""
    _, seats = seated_event
    tier = tier_online_with_cancellation_enabled
    TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=2)
    payment_a = payment_factory(ticket=ticket_factory(tier=tier, seat=seats[0]), amount=Decimal("50.00"))
    payment_b = payment_factory(ticket=ticket_factory(tier=tier, seat=seats[1]), amount=Decimal("30.00"))
    _batch([payment_a, payment_b], "pi_mixed")
    return [payment_a, payment_b]


class TestUnmatchedRefundNotification:
    def test_non_uniform_decline_notifies_staff(self, mixed_price_batch: list[Payment]) -> None:
        """Branch 3's refusal on a mixed-price batch must reach the organizer."""
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        event = _charge_event("pi_mixed", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        notifications = _unmatched_notifications()
        assert len(notifications) == 1, "the organization owner must be told"
        context = notifications[0].context
        assert context["reason"] == "non_uniform"
        assert context["payment_intent_id"] == "pi_mixed"
        assert context["refund_id"] == "re_dashboard"
        assert context["refund_amount"] == "30.00"
        assert context["currency"] == "EUR"
        assert {c["amount"] for c in context["candidates"]} == {"50.00", "30.00"}

    def test_ambiguous_decline_notifies_staff(self, batch_of_4_online_payments: list[Payment]) -> None:
        """Branch 5 declines a uniform batch too — same operator-visible outcome."""
        _batch(batch_of_4_online_payments, "pi_batch")
        refund: dict[str, t.Any] = {"id": "re_ambig", "amount": 4000, "metadata": {}}
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        notifications = _unmatched_notifications()
        assert len(notifications) == 1
        assert notifications[0].context["reason"] == "ambiguous"
        assert len(notifications[0].context["candidates"]) == 4

    def test_metadata_matched_refund_does_not_notify(self, batch_of_4_online_payments: list[Payment]) -> None:
        """Every refund issued through Revel carries ticket_id — it must stay silent.

        A false alarm on each normal refund would be worse than today's silence.
        """
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[2]
        refund: dict[str, t.Any] = {
            "id": "re_revel",
            "amount": 4000,
            "metadata": {"ticket_id": str(target.ticket_id)},
        }
        event = _charge_event("pi_batch", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED, "the normal path must still apply the refund"
        assert _unmatched_notifications() == []

    def test_redelivered_webhook_does_not_notify_twice(self, mixed_price_batch: list[Payment]) -> None:
        """Stripe redelivers; the StripeWebhookEvent dedup row must absorb the repeat."""
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        handle_event(_charge_event("pi_mixed", [refund]))
        assert len(_unmatched_notifications()) == 1

        handle_event(_charge_event("pi_mixed", [refund]))
        assert len(_unmatched_notifications()) == 1, "redelivery must not spam a second notification"

    def test_already_refunded_intent_does_not_notify(self, mixed_price_batch: list[Payment]) -> None:
        """Nothing left to reconcile — a stray refund object on a settled intent is not actionable."""
        Payment.objects.filter(pk__in=[p.pk for p in mixed_price_batch]).update(
            refund_status=Payment.RefundStatus.SUCCEEDED, status=Payment.PaymentStatus.REFUNDED
        )
        refund: dict[str, t.Any] = {"id": "re_late", "amount": 3000, "metadata": {}}
        event = _charge_event("pi_mixed", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        assert _unmatched_notifications() == []

    def test_dispatch_is_deferred_until_after_commit(
        self, mixed_price_batch: list[Payment], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The Notification row commits with the webhook; the Celery dispatch waits for commit.

        The documented trap: a ``.delay()`` fired inside the atomic block would
        race the worker against an uncommitted row.
        """
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        event = _charge_event("pi_mixed", [refund])

        with patch("notifications.tasks.dispatch_notification.delay") as delay:
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                StripeEventHandler(event).handle_charge_refunded(event)
                assert delay.call_count == 0, "dispatch must not fire inside the transaction"
            assert callbacks, "the dispatch must be registered as an on_commit callback"
            notification = _unmatched_notifications()[0]
            delay.assert_any_call(str(notification.id))

    def test_rendered_message_names_what_the_operator_needs(self, mixed_price_batch: list[Payment]) -> None:
        """Title and body must carry the intent, the amount, and the candidate tickets."""
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        event = _charge_event("pi_mixed", [refund])
        StripeEventHandler(event).handle_charge_refunded(event)

        notification = _unmatched_notifications()[0]
        template = get_template(NotificationType.REFUND_UNMATCHED)
        title = template.get_in_app_title(notification)
        body = template.get_in_app_body(notification)

        assert title == "Refund could not be matched to a ticket"
        assert "30.00 EUR" in body
        assert "pi_mixed" in body
        assert "re_dashboard" in body
        for candidate in notification.context["candidates"]:
            assert candidate["holder_email"] in body
            assert candidate["seat_label"] in body
        # The other channels must render too — a missing template file is a silent
        # delivery failure for whichever channel the operator actually uses.
        assert template.get_email_subject(notification)
        assert template.get_email_text_body(notification)
        assert template.get_email_html_body(notification)
        assert template.get_telegram_body(notification)


class TestResolveLink:
    """The notification must be actionable, not merely informative (#744).

    It deep-links to the event's ticket admin filtered to the buyer: every Payment on
    one intent belongs to one buyer, so their email narrows that page to exactly the
    candidates the message lists. No new surface was invented — the route and its
    ``?search=`` handling already exist.
    """

    def test_context_carries_a_link_to_the_filtered_ticket_admin(self, mixed_price_batch: list[Payment]) -> None:
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_mixed", [refund])).handle_charge_refunded(
            _charge_event("pi_mixed", [refund])
        )

        url = _unmatched_notifications()[0].context["resolve_url"]
        ticket = mixed_price_batch[0].ticket

        assert url.startswith(SiteSettings.get_solo().frontend_base_url)
        assert f"/org/{ticket.event.organization.slug}/admin/events/{ticket.event_id}/tickets" in url
        assert f"search={quote(ticket.user.email)}" in url

    def test_every_channel_renders_the_link(self, mixed_price_batch: list[Payment]) -> None:
        """A channel that drops the link leaves that operator with nowhere to go."""
        refund: dict[str, t.Any] = {"id": "re_dashboard", "amount": 3000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_mixed", [refund])).handle_charge_refunded(
            _charge_event("pi_mixed", [refund])
        )

        notification = _unmatched_notifications()[0]
        url = notification.context["resolve_url"]
        template = get_template(NotificationType.REFUND_UNMATCHED)

        html_body = template.get_email_html_body(notification)
        assert html_body is not None, "the HTML email template must exist"

        # Telegram and email have no CTA affordance, so the link has to live in the body.
        assert url in template.get_telegram_body(notification)
        assert url in template.get_email_text_body(notification)
        assert url in html_body

        # In-app deliberately does NOT repeat it: the client renders its own "Review tickets"
        # button from context.resolve_url, so an in-body copy is a second link to the same
        # place in the same card. The context still carries it — that is what the button uses.
        assert url not in template.get_in_app_body(notification)
        assert notification.context["resolve_url"] == url
