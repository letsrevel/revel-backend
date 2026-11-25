"""Canonical lock order on the series-pass activation paths (docs/engineering-notes.md).

parent row -> TicketTier (pk) -> Ticket -> TicketNumberSequence. Both activation paths
backfill (locking tiers) and number tickets (locking the per-org sequence); the tier lock
must come first, or two transactions can wait on each other in a cycle.
"""

import re
import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import RevelUser
from events.exceptions import TicketAlreadyCancelledError
from events.models import EventSeries, HeldSeriesPass, Organization, Payment, Ticket, TicketTier
from events.models.ticket import CancellationBlockReason
from events.service import refund_service, series_pass_service, ticket_service
from events.service.cancellation_service import cancel_ticket_by_user
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.refunds import refund_one_cancelled_event_ticket
from events.tasks.series_pass import materialize_series_pass_holders
from events.tests.test_series_pass.conftest import revel_user  # noqa: F401 - fixture used by held_pass
from events.tests.test_series_pass.test_backfill import (
    _completed_checkout_event,
    _make_covered_event,
    _make_pass,
    _purchase_online,
)
from events.tests.test_series_pass.test_cancellation import (  # noqa: F401 - fixtures
    _PassSetup,
    held_pass,
    online_pass_setup,
    online_series_pass,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_lock_order"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return organization


def _first_lock(queries: list[dict[str, t.Any]], table: str) -> int:
    """Index of the first ``SELECT ... FOR UPDATE`` on ``table`` (fails if there is none)."""
    return next(i for i, q in enumerate(queries) if f'FROM "{table}"' in q["sql"] and "FOR UPDATE" in q["sql"])


def _assert_tiers_locked_before_sequence(queries: list[dict[str, t.Any]]) -> None:
    assert _first_lock(queries, "events_tickettier") < _first_lock(queries, "events_ticketnumbersequence")


def _extend_while_pending(org: Organization, event_series: EventSeries, series_pass: t.Any, slug: str) -> None:
    """Link a new event while the pass is PENDING, so activation has to backfill (lock tiers)."""
    new_event, _, _ = _make_covered_event(org, event_series, series_pass, slug)
    with patch("notifications.signals.series_pass.send_series_pass_extended"):
        materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])


def test_offline_confirm_locks_tiers_before_numbering(
    organization: Organization, event_series: EventSeries, member_user: RevelUser
) -> None:
    series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "lo-off")
    held_pass = SeriesPassPurchaseService(series_pass, member_user).purchase()  # noqa: F811
    assert isinstance(held_pass, HeldSeriesPass)
    _extend_while_pending(organization, event_series, series_pass, "lo-off-new")

    with (
        patch("events.service.series_pass_service.send_series_pass_purchased"),
        CaptureQueriesContext(connection) as ctx,
    ):
        series_pass_service.confirm_held_pass_payment(held_pass)

    _assert_tiers_locked_before_sequence(ctx.captured_queries)


def test_webhook_activation_locks_tiers_before_numbering(
    stripe_connected_organization: Organization, event_series: EventSeries, member_user: RevelUser
) -> None:
    series_pass = _make_pass(stripe_connected_organization, event_series, TicketTier.PaymentMethod.ONLINE, "lo-on")
    _purchase_online(series_pass, member_user, "cs_lock_order")
    _extend_while_pending(stripe_connected_organization, event_series, series_pass, "lo-on-new")

    event = _completed_checkout_event("cs_lock_order")
    with CaptureQueriesContext(connection) as ctx:
        StripeEventHandler(event).handle_checkout_session_completed(event)

    _assert_tiers_locked_before_sequence(ctx.captured_queries)


# --- Payment-first lock order on the cancel, refund and webhook paths (#1431, #1450) ---------------------

# Global order (docs/engineering-notes.md): Payment -> HeldSeriesPass -> SeriesPass -> TicketTier -> Event -> Ticket.
_RANK = {
    "events_payment": 0,
    "events_heldseriespass": 1,
    "events_seriespass": 2,
    "events_tickettier": 3,
    "events_event": 4,
    "events_ticket": 5,
    "events_discountcode": 6,
    "events_membershiprecord": 7,
    "events_ticketnumbersequence": 8,
}
_LOCK_RE = re.compile(r'FROM "(\w+)".*FOR UPDATE(?: OF ((?:"\w+"(?:, )?)+))?', re.DOTALL)


def _locks(queries: list[dict[str, t.Any]]) -> list[str]:
    """Tables locked by the ``FOR UPDATE`` statements, in statement order (``OF`` list when present).

    Fails when a statement that joins other tables locks without ``OF``: that would lock the joined rows too.
    """
    locked: list[str] = []
    for query in queries:
        sql = query["sql"]
        if "FOR UPDATE" not in sql:
            continue
        match = _LOCK_RE.search(sql)
        assert match is not None, sql
        if match.group(2) is None:
            assert " JOIN " not in sql, f"FOR UPDATE without OF on a joined query: {sql}"
            locked.append(match.group(1))
        else:
            locked.extend(re.findall(r'"(\w+)"', match.group(2)))
    return locked


def _assert_in_global_order(queries: list[dict[str, t.Any]], repeats: tuple[str, ...] = ()) -> list[str]:
    """Assert the whole lock sequence follows the global order; return each table once, in first-lock order.

    Every lock counts, not only the first one per table: a later lock on a table that ranks below one
    already taken is an inversion. Only the tables in ``repeats`` may be locked again out of order, and
    only because the transaction already holds those rows (a no-op re-lock, e.g. ``issue_refund`` on its Payment).
    """
    sequence = [name for name in _locks(queries) if name in _RANK]
    highest = -1
    for name in sequence:
        if _RANK[name] < highest:
            assert name in repeats, f"lock on {name} after a later table: {sequence}"
        else:
            highest = _RANK[name]
    return list(dict.fromkeys(_locks(queries)))


class _StripeSpy:
    """Stand-in for ``stripe.Refund.create`` that snapshots the queries run before the call."""

    def __init__(self, ctx: CaptureQueriesContext) -> None:
        self.ctx = ctx
        self.snapshots: list[list[dict[str, t.Any]]] = []

    def __call__(self, **_: t.Any) -> MagicMock:
        self.snapshots.append(list(self.ctx.captured_queries))
        return MagicMock(id=f"re_spy_{len(self.snapshots)}")


def _assert_only_payment_locks_before_stripe(spy: _StripeSpy, allowed: tuple[str, ...] = ("events_payment",)) -> None:
    assert spy.snapshots, "Stripe was not called"
    for snapshot in spy.snapshots:
        assert set(_locks(snapshot)) <= set(allowed), _locks(snapshot)
        assert "events_payment" in _locks(snapshot)


def _run_with_spy(call: t.Callable[[], t.Any]) -> tuple[list[dict[str, t.Any]], _StripeSpy]:
    with CaptureQueriesContext(connection) as ctx:
        spy = _StripeSpy(ctx)
        with patch("stripe.Refund.create", side_effect=spy):
            call()
    return list(ctx.captured_queries), spy


_POLICY = {"tiers": [{"hours_before_event": 0, "refund_percentage": "100"}], "flat_fee": "0"}


@pytest.fixture
def paid_ticket(
    event: t.Any,
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    payment_factory: t.Callable[..., Payment],
) -> Ticket:
    event.start = timezone.now() + timedelta(hours=72)
    event.end = event.start + timedelta(hours=2)
    event.save(update_fields=["start", "end"])
    tier = tier_factory(
        payment_method=TicketTier.PaymentMethod.ONLINE,
        allow_user_cancellation=True,
        refund_policy=_POLICY,
        quantity_sold=1,
    )
    ticket = ticket_factory(tier=tier, refund_policy_snapshot=_POLICY)
    payment_factory(ticket)
    return Ticket.objects.select_related("tier", "event", "user").get(pk=ticket.pk)


def test_user_cancel_locks_payment_then_tier_then_ticket(paid_ticket: Ticket) -> None:
    queries, spy = _run_with_spy(
        lambda: cancel_ticket_by_user(paid_ticket, paid_ticket.user, reason="", now=timezone.now())
    )
    assert _assert_in_global_order(queries)[:3] == ["events_payment", "events_tickettier", "events_ticket"]
    _assert_only_payment_locks_before_stripe(spy)


def test_organizer_cancel_with_refund_locks_payment_then_tier_then_ticket(
    paid_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    queries, spy = _run_with_spy(
        lambda: refund_service.admin_cancel_ticket(
            paid_ticket, cancelled_by=organization_owner_user, reason="x", refund_amount=Decimal("10.00")
        )
    )
    assert _assert_in_global_order(queries)[:3] == ["events_payment", "events_tickettier", "events_ticket"]
    _assert_only_payment_locks_before_stripe(spy)


def test_event_cancel_sweep_locks_payment_then_tier_then_ticket(
    paid_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    queries, spy = _run_with_spy(
        lambda: refund_one_cancelled_event_ticket(str(paid_ticket.pk), str(organization_owner_user.pk))
    )
    assert _assert_in_global_order(queries)[:3] == ["events_payment", "events_tickettier", "events_ticket"]
    _assert_only_payment_locks_before_stripe(spy)


def test_offline_refund_locks_payment_then_tier_then_ticket(
    event: t.Any,
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    payment_factory: t.Callable[..., Payment],
    organization_owner_user: RevelUser,
) -> None:
    tier = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, quantity_sold=1)
    ticket = ticket_factory(tier=tier)
    payment_factory(ticket)
    with CaptureQueriesContext(connection) as ctx:
        ticket_service.mark_offline_ticket_refunded(
            ticket, cancelled_by=organization_owner_user, reason="x", refund_amount=Decimal("5.00")
        )
    assert _assert_in_global_order(ctx.captured_queries)[:3] == ["events_payment", "events_tickettier", "events_ticket"]


def test_series_pass_cancel_holds_only_payment_and_pass_across_stripe(
    online_pass_setup: _PassSetup,  # noqa: F811
    organization_owner_user: RevelUser,
) -> None:
    queries, spy = _run_with_spy(
        lambda: series_pass_service.cancel_held_pass(
            online_pass_setup.held_pass, cancelled_by=organization_owner_user, reason="x"
        )
    )
    # The pass-cancel path locks its Payment again after the pass row; the transaction already holds it.
    tables = _assert_in_global_order(queries, repeats=("events_payment",))
    assert tables[:2] == ["events_payment", "events_heldseriespass"]
    _assert_only_payment_locks_before_stripe(spy, ("events_payment", "events_heldseriespass"))
    assert len(spy.snapshots) == 2
    _assert_payment_lock_of_self_pk_order(queries)


def test_checkout_completed_webhook_locks_payment_then_ticket(
    paid_ticket: Ticket, payment_factory: t.Callable[..., Payment]
) -> None:
    Payment.objects.filter(ticket=paid_ticket).update(
        status=Payment.PaymentStatus.PENDING, stripe_session_id="cs_lo_webhook"
    )
    Ticket.objects.filter(pk=paid_ticket.pk).update(status=Ticket.TicketStatus.PENDING)
    event = _completed_checkout_event("cs_lo_webhook")
    with CaptureQueriesContext(connection) as ctx:
        StripeEventHandler(event).handle_checkout_session_completed(event)
    assert _assert_in_global_order(ctx.captured_queries)[:3] == [
        "events_payment",
        "events_heldseriespass",
        "events_ticket",
    ]
    _assert_payment_lock_of_self_pk_order(ctx.captured_queries)


def _assert_payment_lock_of_self_pk_order(queries: list[dict[str, t.Any]]) -> None:
    """The Payment locking statement locks only the Payment rows (``OF``) and orders them by pk."""
    payment_lock = next(
        q["sql"]
        for q in queries
        if "FOR UPDATE" in q["sql"] and q["sql"].split(" FROM ")[1].startswith('"events_payment"')
    )
    assert 'FOR UPDATE OF "events_payment"' in payment_lock
    assert 'ORDER BY "events_payment"."id"' in payment_lock
    assert " JOIN " not in payment_lock, f"Payment lock must not join other tables: {payment_lock}"


def test_offline_cancel_locks_payment_then_tier_then_ticket(
    event: t.Any,
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    payment_factory: t.Callable[..., Payment],
    organization_owner_user: RevelUser,
) -> None:
    tier = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, quantity_sold=1)
    ticket = ticket_factory(tier=tier)
    payment_factory(ticket)
    with CaptureQueriesContext(connection) as ctx:
        ticket_service.cancel_offline_ticket(ticket, cancelled_by=organization_owner_user, reason="x")
    assert _assert_in_global_order(ctx.captured_queries)[:3] == ["events_payment", "events_tickettier", "events_ticket"]


def test_offline_cancel_without_payment_still_locks_in_order(
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    organization_owner_user: RevelUser,
) -> None:
    tier = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, quantity_sold=1)
    ticket = ticket_factory(tier=tier)
    with CaptureQueriesContext(connection) as ctx:
        ticket_service.cancel_offline_ticket(ticket, cancelled_by=organization_owner_user, reason="x")
    assert _assert_in_global_order(ctx.captured_queries)[:3] == ["events_payment", "events_tickettier", "events_ticket"]


def test_cancel_pending_checkout_locks_payment_rows_in_pk_order(paid_ticket: Ticket) -> None:
    from events.service.pending_checkout import cancel_pending_checkout

    payment = Payment.objects.get(ticket=paid_ticket)
    Payment.objects.filter(pk=payment.pk).update(status=Payment.PaymentStatus.PENDING, stripe_payment_intent_id=None)
    Ticket.objects.filter(pk=paid_ticket.pk).update(status=Ticket.TicketStatus.PENDING)
    with CaptureQueriesContext(connection) as ctx:
        cancel_pending_checkout(str(payment.pk), paid_ticket.user)
    _assert_payment_lock_of_self_pk_order(ctx.captured_queries)


def test_payment_intent_canceled_webhook_locks_payment_rows_in_pk_order(paid_ticket: Ticket) -> None:
    Payment.objects.filter(ticket=paid_ticket).update(
        status=Payment.PaymentStatus.PENDING, stripe_payment_intent_id="pi_lock_order"
    )
    Ticket.objects.filter(pk=paid_ticket.pk).update(status=Ticket.TicketStatus.PENDING)
    obj = {"id": "pi_lock_order"}
    event = MagicMock(spec=stripe.Event)
    event.__iter__.return_value = iter({"type": "payment_intent.canceled", "data": {"object": obj}}.items())
    event.type = "payment_intent.canceled"
    event.data = MagicMock()
    event.data.object = obj
    with CaptureQueriesContext(connection) as ctx:
        StripeEventHandler(event).handle_payment_intent_canceled(event)
    _assert_payment_lock_of_self_pk_order(ctx.captured_queries)


def test_refund_paths_raise_already_cancelled_when_the_ticket_vanished(
    paid_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    """A reclaim can delete the ticket while the caller waits on the Payment lock: refuse, do not IndexError."""
    with patch("events.service.ticket_service.lock_tier_then_ticket", return_value=[]):
        with pytest.raises(TicketAlreadyCancelledError):
            refund_service.admin_cancel_ticket(
                paid_ticket, cancelled_by=organization_owner_user, reason="x", refund_amount=Decimal("0.00")
            )
        with pytest.raises(TicketAlreadyCancelledError):
            ticket_service.mark_offline_ticket_refunded(paid_ticket, cancelled_by=organization_owner_user)
        with pytest.raises(TicketAlreadyCancelledError):
            ticket_service.cancel_offline_ticket(paid_ticket, cancelled_by=organization_owner_user)


def test_user_cancel_blocks_when_the_ticket_vanished(paid_ticket: Ticket) -> None:
    """The expiry sweep can delete the PENDING Payment and ticket while the cancel waits: refuse with a 4xx."""
    from events.service.cancellation_service import CancellationBlocked

    Payment.objects.filter(ticket=paid_ticket).delete()
    Ticket.objects.filter(pk=paid_ticket.pk).delete()
    with patch("stripe.Refund.create") as refund:
        with pytest.raises(CancellationBlocked) as exc_info:
            cancel_ticket_by_user(paid_ticket, paid_ticket.user, reason="", now=timezone.now())
    assert exc_info.value.reason == CancellationBlockReason.ALREADY_CANCELLED
    refund.assert_not_called()


def test_reclaim_routes_lock_only_payment_rows_in_pk_order(paid_ticket: Ticket) -> None:
    from events.tasks.payments import cleanup_expired_payments

    Payment.objects.filter(ticket=paid_ticket).update(
        status=Payment.PaymentStatus.PENDING,
        stripe_payment_intent_id=None,
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    Ticket.objects.filter(pk=paid_ticket.pk).update(status=Ticket.TicketStatus.PENDING)
    with CaptureQueriesContext(connection) as ctx:
        cleanup_expired_payments()
    payment_lock = next(
        q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"] and "events_payment" in q["sql"]
    )
    assert 'FOR UPDATE OF "events_payment"' in payment_lock
    assert 'ORDER BY "events_payment"."id"' in payment_lock


def test_user_cancel_blocks_when_a_ticket_without_payment_vanishes_under_the_lock(paid_ticket: Ticket) -> None:
    """A ticket with no Payment can be deleted by a cascade between the read and the tier/ticket lock."""
    from events.service.cancellation_service import CancellationBlocked

    Payment.objects.filter(ticket=paid_ticket).delete()
    with patch("events.service.ticket_service.lock_tier_then_ticket", return_value=[]):
        with pytest.raises(CancellationBlocked) as exc_info:
            cancel_ticket_by_user(paid_ticket, paid_ticket.user, reason="", now=timezone.now())
    assert exc_info.value.reason == CancellationBlockReason.ALREADY_CANCELLED


def test_charge_refunded_webhook_locks_payment_rows_without_join(paid_ticket: Ticket) -> None:
    Payment.objects.filter(ticket=paid_ticket).update(
        status=Payment.PaymentStatus.SUCCEEDED, stripe_payment_intent_id="pi_refund_lock"
    )
    refund = {"id": "re_lock", "amount": 100, "status": "succeeded", "metadata": {}}
    charge = {"id": "ch_lock", "payment_intent": "pi_refund_lock", "refunds": {"data": [refund]}}
    payload: dict[str, t.Any] = {"id": "evt_lock", "type": "charge.refunded", "account": None, "livemode": False}
    payload["data"] = {"object": charge}
    event = MagicMock(spec=stripe.Event)
    event.id, event.type, event.account, event.livemode = "evt_lock", "charge.refunded", None, False
    event.data = MagicMock()
    event.data.object = charge
    event.keys.return_value = list(payload.keys())
    event.__getitem__.side_effect = payload.__getitem__
    with CaptureQueriesContext(connection) as ctx:
        StripeEventHandler(event).handle_charge_refunded(event)
    _assert_payment_lock_of_self_pk_order(ctx.captured_queries)


def test_refund_updated_webhook_locks_payment_rows_without_join_and_calls_no_stripe(paid_ticket: Ticket) -> None:
    """The refund.* handler locks only the Payment rows (pk order) and makes no outbound Stripe call (#1507)."""
    Payment.objects.filter(ticket=paid_ticket).update(
        status=Payment.PaymentStatus.SUCCEEDED, stripe_payment_intent_id="pi_refund_event_lock"
    )
    refund = {
        "id": "re_event_lock",
        "amount": 4000,
        "status": "succeeded",
        "payment_intent": "pi_refund_event_lock",
        "charge": "ch_event_lock",
        "metadata": {},
    }
    payload = {"id": "evt_event_lock", "type": "refund.updated", "livemode": False, "data": {"object": refund}}
    event = stripe.Event.construct_from(payload, "sk_test")
    with CaptureQueriesContext(connection) as ctx, patch("stripe.Refund.list") as refund_list:
        StripeEventHandler(event).handle_refund_event(event)
    _assert_payment_lock_of_self_pk_order(ctx.captured_queries)
    assert _assert_in_global_order(ctx.captured_queries)[0] == "events_payment"
    refund_list.assert_not_called()
