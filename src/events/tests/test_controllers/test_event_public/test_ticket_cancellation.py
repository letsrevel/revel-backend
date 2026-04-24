"""Integration tests for user-initiated ticket cancellation endpoints.

Covers:
  - GET /events/tickets/{ticket_id}/cancellation-preview
  - POST /events/tickets/{ticket_id}/cancel
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketTier
from events.models.ticket import CancellationSource

pytestmark = pytest.mark.django_db


def _authed(user: RevelUser) -> Client:
    """Return a Django test client authenticated as ``user`` via JWT."""
    c = Client()
    refresh = RefreshToken.for_user(user)
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
    return c


@pytest.fixture
def online_cancellable_tier(
    tier_factory: t.Callable[..., TicketTier],
    event: Event,
) -> TicketTier:
    """An ONLINE tier with a two-step refund policy; event starts in 72h."""
    event.start = timezone.now() + timedelta(hours=72)
    event.save(update_fields=["start"])
    return tier_factory(
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=Decimal("40.00"),
        allow_user_cancellation=True,
        refund_policy={
            "tiers": [
                {"hours_before_event": 48, "refund_percentage": "100"},
                {"hours_before_event": 24, "refund_percentage": "50"},
            ],
            "flat_fee": "0",
        },
    )


class TestCancellationPreview:
    """GET /events/tickets/{id}/cancellation-preview."""

    def test_returns_windows_and_live_quote(
        self,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """200 with can_cancel=True, full refund amount, and two refund windows."""
        ticket = ticket_factory(
            tier=online_cancellable_tier,
            refund_policy_snapshot=online_cancellable_tier.refund_policy,
        )
        payment_factory(ticket, amount=Decimal("40.00"))
        url = reverse("api:ticket_cancellation_preview", kwargs={"ticket_id": str(ticket.id)})
        resp = _authed(ticket.user).get(url)
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_cancel"] is True
        assert Decimal(body["refund_amount"]) == Decimal("40.00")
        assert len(body["windows"]) == 2

    def test_403_for_non_owner(
        self,
        nonmember_user: RevelUser,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
    ) -> None:
        """A user who does not own the ticket gets 403."""
        ticket = ticket_factory(tier=online_cancellable_tier)
        url = reverse("api:ticket_cancellation_preview", kwargs={"ticket_id": str(ticket.id)})
        resp = _authed(nonmember_user).get(url)
        assert resp.status_code == 403

    def test_returns_reason_when_event_started(
        self,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
        event: Event,
    ) -> None:
        """200 with can_cancel=False and reason=event_started when the event has begun."""
        event.start = timezone.now() - timedelta(minutes=5)
        event.save(update_fields=["start"])
        ticket = ticket_factory(tier=online_cancellable_tier)
        url = reverse("api:ticket_cancellation_preview", kwargs={"ticket_id": str(ticket.id)})
        resp = _authed(ticket.user).get(url)
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_cancel"] is False
        assert body["reason"] == "event_started"


class TestCancelMyTicket:
    """POST /events/tickets/{id}/cancel."""

    def test_online_ticket_happy_path(
        self,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """Successful cancellation with Stripe refund returns 200 and updates DB records."""
        ticket = ticket_factory(
            tier=online_cancellable_tier,
            refund_policy_snapshot=online_cancellable_tier.refund_policy,
        )
        payment = payment_factory(
            ticket,
            amount=Decimal("40.00"),
            stripe_payment_intent_id="pi_ok",
        )
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        with patch("stripe.Refund.create") as mock:
            mock.return_value = MagicMock(id="re_ok")
            resp = _authed(ticket.user).post(
                url,
                data={"reason": "double-booked"},
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["refund_amount"]) == Decimal("40.00")
        assert body["refund_status"] == "pending"
        payment.refresh_from_db()
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED
        assert ticket.cancellation_source == CancellationSource.USER
        assert ticket.cancellation_reason == "double-booked"
        assert payment.refund_status == Payment.RefundStatus.PENDING

    def test_free_ticket_no_stripe_call(
        self,
        tier_factory: t.Callable[..., TicketTier],
        ticket_factory: t.Callable[..., Ticket],
        event: Event,
    ) -> None:
        """Cancelling a FREE ticket succeeds without ever calling Stripe."""
        event.start = timezone.now() + timedelta(hours=72)
        event.save(update_fields=["start"])
        tier = tier_factory(
            payment_method=TicketTier.PaymentMethod.FREE,
            price=Decimal("0"),
            allow_user_cancellation=True,
        )
        ticket = ticket_factory(tier=tier)
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        with patch("stripe.Refund.create") as mock:
            resp = _authed(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 200
        assert mock.call_count == 0
        assert resp.json()["refund_status"] is None

    def test_blocks_with_409_when_already_cancelled(
        self,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
    ) -> None:
        """Attempting to cancel an already-cancelled ticket returns 409."""
        ticket = ticket_factory(
            tier=online_cancellable_tier,
            status=Ticket.TicketStatus.CANCELLED,
        )
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        resp = _authed(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 409
        assert "already_cancelled" in resp.content.decode()

    def test_403_for_non_owner(
        self,
        nonmember_user: RevelUser,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
    ) -> None:
        """A user who does not own the ticket gets 403."""
        ticket = ticket_factory(tier=online_cancellable_tier)
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        resp = _authed(nonmember_user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 403

    def test_stripe_failure_returns_502_and_ticket_stays_active(
        self,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A Stripe API error rolls back the transaction and returns 502; ticket remains ACTIVE."""
        ticket = ticket_factory(
            tier=online_cancellable_tier,
            refund_policy_snapshot=online_cancellable_tier.refund_policy,
        )
        payment_factory(ticket, amount=Decimal("40.00"), stripe_payment_intent_id="pi_fail")
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        with patch("stripe.Refund.create", side_effect=stripe.error.StripeError("boom")):
            resp = _authed(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 502
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.ACTIVE

    def test_idempotent_duplicate_returns_409(
        self,
        online_cancellable_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
    ) -> None:
        """Re-cancelling an already-cancelled ticket (idempotency check) returns 409."""
        ticket = ticket_factory(
            tier=online_cancellable_tier,
            refund_policy_snapshot=online_cancellable_tier.refund_policy,
            status=Ticket.TicketStatus.CANCELLED,
        )
        url = reverse("api:cancel_my_ticket", kwargs={"ticket_id": str(ticket.id)})
        resp = _authed(ticket.user).post(url, data={}, content_type="application/json")
        assert resp.status_code == 409
