"""The cascade deletes lock their rows in the global order before the cascade runs (#1538, #1532).

``pending_checkout.lock_for_cascade_delete`` locks Payment, then every TicketTier in scope, then the parent
rows (Organization, EventSeries, Event), then Payment again, then Ticket and DiscountCode. See
docs/engineering-notes.md, "Lock order".
"""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.admin.sites import AdminSite
from django.db import connection, transaction
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.admin.event import EventAdmin, EventSeriesAdmin
from events.admin.organization import OrganizationAdmin
from events.admin.ticket import TicketTierAdmin
from events.models import Event, EventSeries, Organization, Payment, Ticket, TicketTier
from events.models.discount_code import DiscountCode
from events.service import discount_code_service, event_series_service, event_update_service, ticket_service
from events.tests.test_cancel_race_1431_1450 import _Pause, _Runner

EXPIRE = "events.service.pending_checkout.expire_stripe_sessions_best_effort"


@pytest.fixture
def buyer(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


@pytest.fixture
def online_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Online",
        price=Decimal("20.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=100,
    )


def _reserve(tier: TicketTier, user: RevelUser, session_id: str, code: DiscountCode | None = None) -> Ticket:
    """A reservation as the checkout writes it: the tier lock, then a PENDING ticket and a PENDING payment."""
    with transaction.atomic():
        TicketTier.objects.select_for_update().get(pk=tier.pk)
        ticket = Ticket.objects.create(
            event_id=tier.event_id,
            tier=tier,
            user=user,
            guest_name="Ada",
            status=Ticket.TicketStatus.PENDING,
            discount_code=code,
        )
        Payment.objects.create(
            ticket=ticket,
            user=user,
            amount=Decimal("20.00"),
            platform_fee=Decimal("0.00"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            reservation_id=uuid.uuid4(),
            stripe_session_id=session_id,
            expires_at=timezone.now() + timedelta(hours=1),
            raw_response={},
        )
        if code is not None:
            discount_code_service.apply_discount(code, user, 1)
    return ticket


def _code(organization: Organization) -> DiscountCode:
    return DiscountCode.objects.create(
        code="LOCKS",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        max_uses=10,
        is_active=True,
    )


def _first_locks(captured: list[dict[str, t.Any]]) -> list[str]:
    """The tables of the row-lock statements, in the order of their first lock."""
    order: list[str] = []
    for query in captured:
        sql = query["sql"]
        if "FOR UPDATE" not in sql and "FOR NO KEY UPDATE" not in sql:
            continue
        table = sql.split(" FROM ")[1].split()[0].strip('"')
        if table not in order:
            order.append(table)
    return order


def _assert_in_order(locks: list[str], *models: type[t.Any]) -> None:
    tables = [model._meta.db_table for model in models]
    missing = [table for table in tables if table not in locks]
    assert not missing, f"not locked: {missing} (locks: {locks})"
    positions = [locks.index(table) for table in tables]
    assert positions == sorted(positions), f"lock order {locks}, expected {tables}"


def _assert_for_update(captured: list[dict[str, t.Any]], *models: type[t.Any]) -> None:
    """The first lock of each model's rows is ``FOR UPDATE``, not ``FOR NO KEY UPDATE``: the delete removes them."""
    for model in models:
        table = f'"{model._meta.db_table}"'
        sql = next(q["sql"] for q in captured if _lock_table(q["sql"]) == table)
        assert "FOR UPDATE" in sql and "NO KEY" not in sql, sql


def _lock_table(sql: str) -> str | None:
    if "FOR UPDATE" not in sql and "FOR NO KEY UPDATE" not in sql:
        return None
    return sql.split(" FROM ")[1].split()[0]


def _assert_locks_before_delete(captured: list[dict[str, t.Any]]) -> None:
    first_delete = next(i for i, q in enumerate(captured) if q["sql"].startswith("DELETE"))
    last_lock = max(i for i, q in enumerate(captured) if "FOR UPDATE" in q["sql"] and "DELETE" not in q["sql"])
    assert last_lock < first_delete


@pytest.mark.django_db
class TestLockOrder:
    """Each delete path takes Payment -> TicketTier -> parents -> Ticket -> DiscountCode, before any DELETE."""

    def test_event_delete(self, online_tier: TicketTier, organization: Organization, buyer: RevelUser) -> None:
        code = _code(organization)
        _reserve(online_tier, buyer, "cs_event", code)

        with patch(EXPIRE), CaptureQueriesContext(connection) as ctx:
            event_update_service.delete_event(online_tier.event)

        _assert_in_order(_first_locks(ctx.captured_queries), Payment, TicketTier, Event, Ticket, DiscountCode)
        _assert_for_update(ctx.captured_queries, TicketTier, Event)
        _assert_locks_before_delete(ctx.captured_queries)
        assert not Event.objects.filter(pk=online_tier.event_id).exists()
        code.refresh_from_db()
        assert code.times_used == 0  # the use of the pending checkout goes back

    def test_tier_delete(self, online_tier: TicketTier, organization: Organization, buyer: RevelUser) -> None:
        code = _code(organization)
        _reserve(online_tier, buyer, "cs_tier", code)

        with patch(EXPIRE), CaptureQueriesContext(connection) as ctx:
            ticket_service.delete_ticket_tier(online_tier)

        locks = _first_locks(ctx.captured_queries)
        _assert_in_order(locks, Payment, TicketTier, Ticket, DiscountCode)
        assert Event._meta.db_table not in locks  # the event stays: a tier delete does not lock it
        _assert_for_update(ctx.captured_queries, TicketTier)
        _assert_locks_before_delete(ctx.captured_queries)
        code.refresh_from_db()
        assert code.times_used == 0

    def test_series_delete(
        self, online_tier: TicketTier, organization: Organization, buyer: RevelUser, event_series: EventSeries
    ) -> None:
        Event.objects.filter(pk=online_tier.event_id).update(event_series=event_series)
        _reserve(online_tier, buyer, "cs_series", _code(organization))

        with patch(EXPIRE), CaptureQueriesContext(connection) as ctx:
            event_series_service.delete_event_series(event_series)

        _assert_in_order(
            _first_locks(ctx.captured_queries), Payment, TicketTier, EventSeries, Event, Ticket, DiscountCode
        )
        _assert_for_update(ctx.captured_queries, TicketTier, EventSeries, Event)
        _assert_locks_before_delete(ctx.captured_queries)

    def test_organization_admin_delete(
        self, online_tier: TicketTier, organization: Organization, buyer: RevelUser, event_series: EventSeries
    ) -> None:
        Event.objects.filter(pk=online_tier.event_id).update(event_series=event_series)
        _reserve(online_tier, buyer, "cs_org", _code(organization))
        admin = OrganizationAdmin(Organization, AdminSite())

        with patch(EXPIRE), CaptureQueriesContext(connection) as ctx:
            admin.delete_model(MagicMock(), organization)

        _assert_in_order(
            _first_locks(ctx.captured_queries),
            Payment,
            TicketTier,
            Organization,
            EventSeries,
            Event,
            Ticket,
            DiscountCode,
        )
        _assert_for_update(ctx.captured_queries, TicketTier, Organization, EventSeries, Event)
        _assert_locks_before_delete(ctx.captured_queries)
        assert not Organization.objects.filter(pk=organization.pk).exists()

    @pytest.mark.parametrize("kind", ["event", "series", "tier"])
    def test_admin_bulk_delete_locks_the_whole_selection_first(
        self,
        organization: Organization,
        buyer: RevelUser,
        event: Event,
        public_event: Event,
        kind: str,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """One request deletes several rows: their Payment, tier and parent rows are locked together, in pk order.

        Each delete service locks only its own rows, so a loop alone would lock them one delete at a time and
        not in pk order. One checkout can span events, and a series pass locks the tiers of several events in
        pk order, so the loop could deadlock with them.
        """
        sessions, tiers, series = [], [], []
        for i, target in enumerate((event, public_event)):
            group = EventSeries.objects.create(organization=organization, name=f"Bulk {i}", slug=f"bulk-{i}")
            Event.objects.filter(pk=target.pk).update(event_series=group)
            series.append(group)
            tiers.append(TicketTier.objects.create(event=target, name="T", price=Decimal("5.00"), currency="EUR"))
            sessions.append(f"cs_bulk_{target.pk}")
            _reserve(tiers[-1], buyer, sessions[-1])
        site = AdminSite()
        selected: dict[str, tuple[t.Any, QuerySet[t.Any], list[type[t.Any]]]] = {
            "event": (EventAdmin(Event, site), Event.objects.filter(pk__in=[event.pk, public_event.pk]), [Event]),
            "series": (
                EventSeriesAdmin(EventSeries, site),
                EventSeries.objects.filter(pk__in=[group.pk for group in series]),
                [EventSeries, Event],
            ),
            "tier": (TicketTierAdmin(TicketTier, site), TicketTier.objects.filter(pk__in=[x.pk for x in tiers]), []),
        }
        admin, queryset, parents = selected[kind]

        with patch(EXPIRE) as expire, CaptureQueriesContext(connection) as ctx:
            with django_capture_on_commit_callbacks(execute=True):
                admin.delete_queryset(MagicMock(), queryset)

        locks = [table for q in ctx.captured_queries if (table := _lock_table(q["sql"]))]
        expected: list[type[t.Any]] = [Payment, TicketTier, *parents]
        assert locks[: len(expected)] == [f'"{model._meta.db_table}"' for model in expected], locks
        first = next(q["sql"] for q in ctx.captured_queries if _lock_table(q["sql"]))
        assert "ORDER BY" in first
        assert not TicketTier.objects.filter(pk__in=[x.pk for x in tiers]).exists()
        expired = {sid for call in expire.call_args_list for sid in call.args[0]}
        assert expired == set(sessions)


def _payment_lock(sql: str) -> bool:
    return "FOR UPDATE" in sql and sql.split(" FROM ")[1].split()[0] == '"events_payment"'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["event", "tier"])
def test_a_reservation_that_commits_during_the_delete_gets_its_session_expired(
    online_tier: TicketTier, buyer: RevelUser, path: str
) -> None:
    """A reservation commits after the delete's first Payment lock and before its tier lock (#1532).

    The delete locks the Payment rows again after the tier lock, so the new session is expired too. Without
    the second lock it stays open, and the cascade removes the rows that a payment of it would need.
    ``transaction=True``: each thread has its own connection and must see the other's commit, and the
    expire callback runs only on a real commit.
    """
    runner = _Runner()
    pause = _Pause(_payment_lock)

    def delete() -> None:
        with transaction.atomic():
            if path == "event":
                event_update_service.delete_event(Event.objects.get(pk=online_tier.event_id))
            else:
                ticket_service.delete_ticket_tier(TicketTier.objects.get(pk=online_tier.pk))

    with patch(EXPIRE) as expire:
        runner.start("delete", delete, pause)
        assert pause.reached.wait(timeout=10), "the delete never reached its first Payment lock"
        _reserve(online_tier, buyer, "cs_late")  # commits at once: the delete holds no tier lock yet
        pause.release.set()
        runner.join()

    assert runner.outcome["delete"][0] == "ok", runner.outcome["delete"]
    expired = {sid for call in expire.call_args_list for sid in call.args[0]}
    assert expired == {"cs_late"}
    assert not Payment.objects.filter(stripe_session_id="cs_late").exists()
