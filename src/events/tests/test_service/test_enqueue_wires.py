"""Verify ``enqueue_waitlist_processing`` fires from every capacity-freeing path.

These are wiring tests — they assert the call site is reached for each modified
path. The full waitlist processing pipeline is exercised by its own dedicated
tests; here we only care that the enqueue helper is invoked with the correct
``event_id`` from every place that frees capacity.
"""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Task 12 — cancel_ticket_by_user
# ---------------------------------------------------------------------------


def test_user_ticket_cancellation_enqueues_waitlist(
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    event: Event,
) -> None:
    """``cancel_ticket_by_user`` enqueues processing for the freed event."""
    from events.service.cancellation_service import cancel_ticket_by_user

    event.start = timezone.now() + timedelta(hours=72)
    event.end = event.start + timedelta(hours=1)
    event.save(update_fields=["start", "end"])

    tier = tier_factory(
        payment_method=TicketTier.PaymentMethod.FREE,
        price=Decimal("0"),
        allow_user_cancellation=True,
    )
    ticket = ticket_factory(tier=tier)
    tier.quantity_sold = 1
    tier.save(update_fields=["quantity_sold"])

    with mock.patch("events.service.cancellation_service.enqueue_waitlist_processing") as mocked:
        cancel_ticket_by_user(ticket, ticket.user, reason="changed mind", now=timezone.now())

    mocked.assert_called_once_with(ticket.event_id)


# ---------------------------------------------------------------------------
# Task 13 — Stripe webhooks (refund + payment_intent canceled)
# ---------------------------------------------------------------------------


def test_stripe_refund_webhook_enqueues_waitlist(
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """``handle_charge_refunded`` enqueues processing when a ticket flips to CANCELLED."""
    from unittest.mock import MagicMock

    import stripe

    from events.service.stripe_webhooks import StripeEventHandler

    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("25.00")
    tier.quantity_sold = 5
    tier.save()

    ticket = Ticket.objects.create(
        guest_name="Test Guest",
        event=event,
        tier=tier,
        user=organization_owner_user,
        status=Ticket.TicketStatus.ACTIVE,
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=organization_owner_user,
        stripe_session_id="cs_test_enq",
        stripe_payment_intent_id="pi_test_enq",
        amount=Decimal("25.00"),
        platform_fee=Decimal("1.25"),
        currency="EUR",
        status=Payment.PaymentStatus.SUCCEEDED,
        raw_response={},
    )

    refund_amount_cents = int(payment.amount * 100)
    event_dict_data: dict[str, t.Any] = {
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_test_enq",
                "payment_intent": "pi_test_enq",
                "refunds": {"data": [{"id": "re_test_enq", "amount": refund_amount_cents, "metadata": {}}]},
            }
        },
    }
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_dict_data.items())
    mock_event.type = event_dict_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = event_dict_data["data"]["object"]

    handler = StripeEventHandler(mock_event)

    with mock.patch("events.service.stripe_webhooks.enqueue_waitlist_processing") as mocked:
        handler.handle_charge_refunded(mock_event)

    mocked.assert_called_with(ticket.event_id)


def test_stripe_payment_intent_canceled_enqueues_waitlist(
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """``handle_payment_intent_canceled`` enqueues processing for each cancelled ticket."""
    from unittest.mock import MagicMock

    import stripe

    from events.service.stripe_webhooks import StripeEventHandler

    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("25.00")
    tier.quantity_sold = 5
    tier.save()

    ticket = Ticket.objects.create(
        guest_name="Pending Guest",
        event=event,
        tier=tier,
        user=organization_owner_user,
        status=Ticket.TicketStatus.PENDING,
    )
    Payment.objects.create(
        ticket=ticket,
        user=organization_owner_user,
        stripe_session_id="cs_test_pi_cancel",
        stripe_payment_intent_id="pi_cancel_enq",
        amount=Decimal("25.00"),
        platform_fee=Decimal("1.25"),
        currency="EUR",
        status=Payment.PaymentStatus.PENDING,
        raw_response={},
    )

    event_dict_data: dict[str, t.Any] = {
        "type": "payment_intent.canceled",
        "data": {"object": {"id": "pi_cancel_enq", "status": "canceled"}},
    }
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_dict_data.items())
    mock_event.type = event_dict_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = event_dict_data["data"]["object"]

    handler = StripeEventHandler(mock_event)

    with mock.patch("events.service.stripe_webhooks.enqueue_waitlist_processing") as mocked:
        handler.handle_payment_intent_canceled(mock_event)

    mocked.assert_called_with(ticket.event_id)


def test_stripe_payment_intent_canceled_enqueues_once_per_event(
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """A batch cancellation (N pending payments, same event) enqueues exactly once."""
    from unittest.mock import MagicMock

    import stripe

    from events.service.stripe_webhooks import StripeEventHandler

    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("25.00")
    tier.quantity_sold = 5
    tier.save()

    for n in range(3):
        ticket = Ticket.objects.create(
            guest_name=f"Pending Guest {n}",
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
        )
        Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id=f"cs_batch_pi_cancel_{n}",
            stripe_payment_intent_id="pi_cancel_batch",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    event_dict_data: dict[str, t.Any] = {
        "type": "payment_intent.canceled",
        "data": {"object": {"id": "pi_cancel_batch", "status": "canceled"}},
    }
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_dict_data.items())
    mock_event.type = event_dict_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = event_dict_data["data"]["object"]

    handler = StripeEventHandler(mock_event)

    with mock.patch("events.service.stripe_webhooks.enqueue_waitlist_processing") as mocked:
        handler.handle_payment_intent_canceled(mock_event)

    mocked.assert_called_once_with(event.id)


def test_stripe_refund_webhook_enqueues_once_per_event(
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """A full-batch refund spanning N payments for one event enqueues exactly once."""
    from unittest.mock import MagicMock

    import stripe

    from events.service.stripe_webhooks import StripeEventHandler

    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("25.00")
    tier.quantity_sold = 5
    tier.save()

    for n in range(3):
        ticket = Ticket.objects.create(
            guest_name=f"Batch Guest {n}",
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id=f"cs_batch_refund_{n}",
            stripe_payment_intent_id="pi_batch_refund",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.SUCCEEDED,
            raw_response={},
        )

    # Full-batch refund: amount equals sum of payment amounts.
    refund_amount_cents = int(Decimal("75.00") * 100)
    event_dict_data: dict[str, t.Any] = {
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_batch_enq",
                "payment_intent": "pi_batch_refund",
                "refunds": {"data": [{"id": "re_batch_enq", "amount": refund_amount_cents, "metadata": {}}]},
            }
        },
    }
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_dict_data.items())
    mock_event.type = event_dict_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = event_dict_data["data"]["object"]

    handler = StripeEventHandler(mock_event)

    with mock.patch("events.service.stripe_webhooks.enqueue_waitlist_processing") as mocked:
        handler.handle_charge_refunded(mock_event)

    mocked.assert_called_once_with(event.id)


# ---------------------------------------------------------------------------
# Task 14 — Admin mark_ticket_refunded and cancel_ticket endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    """Offline-payment tier on the default event."""
    return TicketTier.objects.create(
        event=event,
        name="Admin Offline",
        price=25.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def offline_ticket(event: Event, offline_tier: TicketTier, public_user: RevelUser) -> Ticket:
    """A pending offline ticket eligible for admin cancel/refund."""
    return Ticket.objects.create(
        guest_name="Admin Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def owner_jwt_client(organization_owner_user: RevelUser) -> Client:
    """Local API client for the organization owner (avoids cross-conftest fixtures)."""
    from ninja_jwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def test_admin_mark_ticket_refunded_enqueues_waitlist(
    owner_jwt_client: Client,
    event: Event,
    offline_ticket: Ticket,
) -> None:
    """The admin mark-refunded endpoint enqueues waitlist processing."""
    url = reverse(
        "api:mark_ticket_refunded",
        kwargs={"event_id": event.pk, "ticket_id": offline_ticket.pk},
    )

    with mock.patch("events.controllers.event_admin.tickets.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.post(url, data={}, content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(offline_ticket.event_id)


def test_admin_cancel_ticket_enqueues_waitlist(
    owner_jwt_client: Client,
    event: Event,
    offline_ticket: Ticket,
) -> None:
    """The admin cancel endpoint enqueues waitlist processing."""
    url = reverse(
        "api:cancel_ticket",
        kwargs={"event_id": event.pk, "ticket_id": offline_ticket.pk},
    )

    with mock.patch("events.controllers.event_admin.tickets.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.post(url, data={}, content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(offline_ticket.event_id)


# ---------------------------------------------------------------------------
# Task 19 — cancel_pending_checkout
# ---------------------------------------------------------------------------


def test_cancel_pending_checkout_enqueues_waitlist(
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """``stripe_service.cancel_pending_checkout`` enqueues processing for the freed event."""
    from events.service.stripe_service import cancel_pending_checkout

    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("25.00")
    tier.quantity_sold = 1
    tier.save()

    ticket = Ticket.objects.create(
        guest_name="Cart Guest",
        event=event,
        tier=tier,
        user=organization_owner_user,
        status=Ticket.TicketStatus.PENDING,
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=organization_owner_user,
        stripe_session_id=f"cs_cancel_{uuid.uuid4().hex}",
        amount=Decimal("25.00"),
        platform_fee=Decimal("1.25"),
        currency="EUR",
        status=Payment.PaymentStatus.PENDING,
        raw_response={},
    )

    expected_event_id = ticket.event_id

    with mock.patch("events.service.stripe_service.enqueue_waitlist_processing") as mocked:
        cancel_pending_checkout(str(payment.id), organization_owner_user)

    mocked.assert_called_once_with(expected_event_id)
