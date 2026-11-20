"""Races between ticket cancellation paths and the ``checkout.session.completed`` webhook (#1431, #1450).

Every transaction that changes a ticket with a ``Payment`` locks the Payment first
(Payment -> TicketTier -> Ticket, see ``docs/engineering-notes.md``). These tests run the paths
on separate connections and pause one of them while it holds its locks.

``transaction=True`` is required for all tests here: each thread uses its own connection and
must see the other's real commits and lock waits, which the rollback-wrapped default
``django_db`` transaction (one connection, never committed) cannot provide.
"""

import threading
import time
import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from accounts.models import RevelUser
from events.exceptions import TicketAlreadyCancelledError
from events.models import Event, Payment, Refund, Ticket, TicketTier
from events.service import refund_service
from events.service.cancellation_service import CancellationBlocked, cancel_ticket_by_user
from events.service.stripe_webhooks import StripeEventHandler
from events.tests.test_series_pass.test_backfill import _completed_checkout_event

_Wrapper = t.Callable[..., t.Any]
_SESSION = "cs_race_1431"
_POLICY = {"tiers": [{"hours_before_event": 0, "refund_percentage": "100"}], "flat_fee": "0"}


class _Pause:
    """Pause a connection right after the first statement matching ``predicate`` ran (its locks stay held)."""

    def __init__(self, predicate: t.Callable[[str], bool]) -> None:
        self.predicate = predicate
        self.reached = threading.Event()
        self.release = threading.Event()
        self._fired = False

    def __call__(self, execute: t.Callable[..., t.Any], sql: str, params: t.Any, many: bool, context: t.Any) -> t.Any:
        result = execute(sql, params, many, context)
        if not self._fired and self.predicate(sql):
            self._fired = True
            self.reached.set()
            self.release.wait(timeout=20)
        return result


def _first_lock(sql: str) -> bool:
    return "FOR UPDATE" in sql


def _payment_update(sql: str) -> bool:
    return sql.startswith('UPDATE "events_payment"')


def _ticket_update(sql: str) -> bool:
    return sql.startswith('UPDATE "events_ticket"')


class _Runner:
    """Run callables on their own threads/connections and collect result or exception per key."""

    def __init__(self) -> None:
        self.outcome: dict[str, tuple[str, t.Any]] = {}
        self.threads: list[threading.Thread] = []

    def start(self, key: str, fn: t.Callable[[], t.Any], pause: _Pause | None = None) -> threading.Thread:
        def target() -> None:
            try:
                if pause is None:
                    self.outcome[key] = ("ok", fn())
                else:
                    with connection.execute_wrapper(pause):
                        self.outcome[key] = ("ok", fn())
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertions
                self.outcome[key] = ("err", exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "thread did not finish (stuck lock wait)"


@pytest.fixture
def tier(event: Event) -> TicketTier:
    event.start = timezone.now() + timedelta(hours=72)
    event.end = event.start + timedelta(hours=2)
    event.save(update_fields=["start", "end"])
    return TicketTier.objects.create(
        event=event,
        name="Race",
        price=Decimal("40.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        allow_user_cancellation=True,
        refund_policy=_POLICY,
        quantity_sold=1,
    )


@pytest.fixture
def stripe_refund() -> t.Iterator[MagicMock]:
    with patch("stripe.Refund.create") as mock_create:
        mock_create.return_value.id = "re_race"
        yield mock_create


def _make_ticket(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    tier: TicketTier,
    *,
    status: Ticket.TicketStatus,
    payment_status: Payment.PaymentStatus = Payment.PaymentStatus.SUCCEEDED,
    **payment_kwargs: t.Any,
) -> tuple[Ticket, Payment]:
    ticket = ticket_factory(tier=tier, status=status, refund_policy_snapshot=_POLICY)
    return ticket, payment_factory(ticket, status=payment_status, **payment_kwargs)


def _user_cancel(ticket_pk: t.Any) -> t.Any:
    ticket = Ticket.objects.select_related("tier", "event", "user").get(pk=ticket_pk)
    return cancel_ticket_by_user(ticket, ticket.user, reason="race", now=timezone.now())


def _webhook() -> None:
    event = _completed_checkout_event(_SESSION)
    StripeEventHandler(event).handle_checkout_session_completed(event)


def _assert_cancelled_once(ticket: Ticket, tier: TicketTier) -> None:
    ticket.refresh_from_db()
    tier.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CANCELLED
    assert tier.quantity_sold == 0


@pytest.mark.django_db(transaction=True)
def test_webhook_first_then_user_cancel_does_not_deadlock(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    tier: TicketTier,
    stripe_refund: MagicMock,
) -> None:
    """The webhook holds the Payment when the user cancels: the cancel waits, then cancels the ACTIVE ticket."""
    ticket, payment = _make_ticket(
        ticket_factory,
        payment_factory,
        tier,
        status=Ticket.TicketStatus.PENDING,
        stripe_session_id=_SESSION,
        stripe_payment_intent_id=None,
        payment_status=Payment.PaymentStatus.PENDING,
    )
    runner = _Runner()
    pause = _Pause(_payment_update)
    runner.start("webhook", _webhook, pause)
    assert pause.reached.wait(timeout=10), "webhook never reached its Payment update"
    runner.start("cancel", lambda: _user_cancel(ticket.pk))
    time.sleep(0.7)  # let the cancel reach its first lock wait
    pause.release.set()
    runner.join()

    assert runner.outcome["webhook"][0] == "ok", runner.outcome["webhook"]
    assert runner.outcome["cancel"][0] == "ok", runner.outcome["cancel"]
    _assert_cancelled_once(ticket, tier)
    payment.refresh_from_db()
    assert payment.status == Payment.PaymentStatus.SUCCEEDED


@pytest.mark.django_db(transaction=True)
def test_user_cancel_first_then_webhook_keeps_ticket_cancelled(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    tier: TicketTier,
    stripe_refund: MagicMock,
) -> None:
    """A late payment confirmation must not bring back a ticket the user just cancelled (#1431)."""
    ticket, payment = _make_ticket(
        ticket_factory,
        payment_factory,
        tier,
        status=Ticket.TicketStatus.PENDING,
        stripe_session_id=_SESSION,
        stripe_payment_intent_id=None,
        payment_status=Payment.PaymentStatus.PENDING,
    )
    runner = _Runner()
    pause = _Pause(_ticket_update)
    runner.start("cancel", lambda: _user_cancel(ticket.pk), pause)
    assert pause.reached.wait(timeout=10), "cancel never reached its ticket update"
    runner.start("webhook", _webhook)
    time.sleep(0.7)  # let the webhook read (and, if it can, write) while the cancel is open
    pause.release.set()
    runner.join()

    assert runner.outcome["cancel"][0] == "ok", runner.outcome["cancel"]
    assert runner.outcome["webhook"][0] == "ok", runner.outcome["webhook"]
    _assert_cancelled_once(ticket, tier)
    payment.refresh_from_db()
    assert payment.status == Payment.PaymentStatus.SUCCEEDED


@pytest.mark.django_db(transaction=True)
def test_user_cancel_and_organizer_cancel_do_not_deadlock(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    tier: TicketTier,
    stripe_refund: MagicMock,
    organization_owner_user: RevelUser,
) -> None:
    """Both cancels take Payment first (#1450): one wins, the other is refused, counts stay right."""
    ticket, payment = _make_ticket(ticket_factory, payment_factory, tier, status=Ticket.TicketStatus.ACTIVE)

    def organizer_cancel() -> t.Any:
        fresh = Ticket.objects.select_related("tier").get(pk=ticket.pk)
        return refund_service.admin_cancel_ticket(
            fresh, cancelled_by=organization_owner_user, reason="race", refund_amount=Decimal("10.00")
        )

    runner = _Runner()
    pause = _Pause(_first_lock)
    runner.start("user", lambda: _user_cancel(ticket.pk), pause)
    assert pause.reached.wait(timeout=10), "user cancel never took a lock"
    runner.start("organizer", organizer_cancel)
    time.sleep(0.7)  # let the organizer cancel reach its first lock wait
    pause.release.set()
    runner.join()

    assert runner.outcome["user"][0] == "ok", runner.outcome["user"]
    kind, error = runner.outcome["organizer"]
    assert kind == "err", "organizer cancel must be refused once the user cancel committed"
    assert isinstance(error, TicketAlreadyCancelledError), repr(error)
    _assert_cancelled_once(ticket, tier)
    assert stripe_refund.call_count == 1
    assert Refund.objects.filter(payment=payment).count() == 1


@pytest.mark.django_db(transaction=True)
def test_check_in_during_refund_rolls_back_the_cancel_step(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    tier: TicketTier,
    stripe_refund: MagicMock,
) -> None:
    """A check-in that lands while Stripe refunds: the ticket stays checked in, the Refund row is kept."""
    ticket, payment = _make_ticket(ticket_factory, payment_factory, tier, status=Ticket.TicketStatus.ACTIVE)
    in_stripe = threading.Event()
    release = threading.Event()

    def slow_refund(**_: t.Any) -> MagicMock:
        in_stripe.set()
        release.wait(timeout=20)
        return MagicMock(id="re_race")

    stripe_refund.side_effect = slow_refund
    runner = _Runner()
    with patch("events.service.cancellation_service.logger") as log:
        runner.start("cancel", lambda: _user_cancel(ticket.pk))
        assert in_stripe.wait(timeout=10), "cancel never reached Stripe"
        # Only the Payment is locked across Stripe: the ticket row is free, so this does not block.
        assert Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.CHECKED_IN) == 1
        release.set()
        runner.join()

    kind, error = runner.outcome["cancel"]
    assert kind == "err"
    assert isinstance(error, CancellationBlocked), repr(error)
    ticket.refresh_from_db()
    tier.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert tier.quantity_sold == 1
    assert Refund.objects.filter(payment=payment, stripe_refund_id="re_race").count() == 1
    assert any(c.args and c.args[0] == "cancel_rolled_back_after_refund" for c in log.warning.call_args_list)
