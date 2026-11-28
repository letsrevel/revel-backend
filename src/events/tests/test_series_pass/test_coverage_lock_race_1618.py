"""A new tier link and a change that breaks the coverage rule cannot both pass (#1618).

``add_tier_links`` locks the pass, the tiers and the events (``FOR NO KEY UPDATE``). A change of the event type or
series (#1598), a stricter tier rule (#1341) and a looser pass rule (#1332) lock their own row before they read
the links. Each test pauses one path while it holds its lock, and checks that the other path waits and then
refuses.

``transaction=True`` is required: each thread uses its own connection and must see the other's real commits and
lock waits, which the rollback-wrapped default ``django_db`` transaction (one connection) cannot provide.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.exceptions import SeriesPassCoverageError
from events.models import Event, SeriesPass, SeriesPassTierLink, TicketTier
from events.service import series_pass_service
from events.tests.test_cancel_race_1431_1450 import _assert_waits_on_lock, _Pause, _Runner

pytestmark = pytest.mark.django_db(transaction=True)


def _uncovered(series_pass: SeriesPass) -> tuple[Event, TicketTier]:
    series = series_pass.event_series
    event = Event.objects.create(
        organization=series.organization,
        name="Race event",
        slug="race-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=5),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=event,
        name="Race tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    return event, tier


def _row_lock_on(table: str) -> t.Callable[[str], bool]:
    return lambda sql: "FOR NO KEY UPDATE" in sql and f'FROM "{table}"' in sql


def _link(series_pass: SeriesPass, event: Event, tier: TicketTier) -> t.Callable[[], t.Any]:
    def run() -> t.Any:
        sp = SeriesPass.objects.get(pk=series_pass.pk)
        return series_pass_service.add_tier_links(sp, [{"event_id": event.pk, "tier_id": tier.pk}], materialize=False)

    return run


_Change = t.Callable[[SeriesPass, Event, TicketTier], t.Callable[[], None]]


def _make_event_private(series_pass: SeriesPass, event: Event, tier: TicketTier) -> t.Callable[[], None]:
    def run() -> None:
        with transaction.atomic():
            fresh = Event.objects.get(pk=event.pk)
            fresh.event_type = Event.EventType.PRIVATE
            fresh.save()

    return run


def _make_tier_members_only(series_pass: SeriesPass, event: Event, tier: TicketTier) -> t.Callable[[], None]:
    def run() -> None:
        with transaction.atomic():
            fresh = TicketTier.objects.get(pk=tier.pk)
            fresh.purchasable_by = TicketTier.PurchasableBy.MEMBERS
            fresh.save()

    return run


def _make_pass_public(series_pass: SeriesPass, event: Event, tier: TicketTier) -> t.Callable[[], None]:
    """The pass side (#1332): a members pass that becomes public no longer admits only the buyers of a members tier."""

    def run() -> None:
        with transaction.atomic():
            fresh = SeriesPass.objects.get(pk=series_pass.pk)
            fresh.purchasable_by = TicketTier.PurchasableBy.PUBLIC
            fresh.save()

    return run


def _setup(series_pass: SeriesPass, change: _Change) -> tuple[Event, TicketTier]:
    event, tier = _uncovered(series_pass)
    if change is _make_pass_public:
        members = TicketTier.PurchasableBy.MEMBERS
        SeriesPass.objects.filter(pk=series_pass.pk).update(purchasable_by=members)
        TicketTier.objects.filter(pk=tier.pk).update(purchasable_by=members)
    return event, tier


def _state(series_pass: SeriesPass, event: Event, tier: TicketTier) -> tuple[str, str, str]:
    return (
        Event.objects.get(pk=event.pk).event_type,
        TicketTier.objects.get(pk=tier.pk).purchasable_by,
        SeriesPass.objects.get(pk=series_pass.pk).purchasable_by,
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_make_event_private, id="event-type"),
        pytest.param(_make_tier_members_only, id="tier-rule"),
        pytest.param(_make_pass_public, id="pass-rule"),
    ],
)
def test_a_change_waits_for_a_concurrent_link_and_is_then_refused(series_pass: SeriesPass, change: _Change) -> None:
    event, tier = _setup(series_pass, change)
    before = _state(series_pass, event, tier)
    pause = _Pause(_row_lock_on("events_event"))  # the last lock of the link path
    runner = _Runner()

    runner.start("link", _link(series_pass, event, tier), pause)
    assert pause.reached.wait(timeout=10)
    changer = runner.start("change", change(series_pass, event, tier))
    _assert_waits_on_lock(changer)
    pause.release.set()
    runner.join()

    assert runner.outcome["link"][0] == "ok", runner.outcome["link"]
    status, exc = runner.outcome["change"]
    assert status == "err" and isinstance(exc, ValidationError), runner.outcome["change"]
    assert SeriesPassTierLink.objects.filter(event=event, tier=tier).exists()
    assert _state(series_pass, event, tier) == before


@pytest.mark.parametrize(
    ("change", "table", "refusal"),
    [
        pytest.param(_make_event_private, "events_event", SeriesPassCoverageError, id="event-type"),
        pytest.param(_make_tier_members_only, "events_tickettier", ValidationError, id="tier-rule"),
        pytest.param(_make_pass_public, "events_seriespass", ValidationError, id="pass-rule"),
    ],
)
def test_a_link_waits_for_a_concurrent_change_and_is_then_refused(
    series_pass: SeriesPass, change: _Change, table: str, refusal: type[Exception]
) -> None:
    event, tier = _setup(series_pass, change)
    pause = _Pause(_row_lock_on(table))  # the lock of the change path, before its links query
    runner = _Runner()

    runner.start("change", change(series_pass, event, tier), pause)
    assert pause.reached.wait(timeout=10)
    linker = runner.start("link", _link(series_pass, event, tier))
    _assert_waits_on_lock(linker)
    pause.release.set()
    runner.join()

    assert runner.outcome["change"][0] == "ok", runner.outcome["change"]
    status, exc = runner.outcome["link"]
    assert status == "err" and isinstance(exc, refusal), runner.outcome["link"]
    assert not SeriesPassTierLink.objects.filter(event=event).exists()


def test_an_event_edit_with_new_links_locks_the_pass_and_the_tier_before_the_event(
    series_pass: SeriesPass, organization_owner_user: RevelUser
) -> None:
    """The global lock order is SeriesPass -> TicketTier -> Event. The event save locks the event row, so the edit
    must lock the pass and the tier of its new links first, or it can deadlock with ``add_tier_links``."""
    event, tier = _uncovered(series_pass)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(organization_owner_user).access_token!s}")  # type: ignore[attr-defined]
    body = {"name": event.name, "series_pass_links": [{"series_pass_id": str(series_pass.pk), "tier_id": str(tier.pk)}]}

    with CaptureQueriesContext(connection) as ctx:
        response = client.put(
            reverse("api:edit_event", kwargs={"event_id": event.pk}),
            data=orjson.dumps(body),
            content_type="application/json",
        )

    assert response.status_code == 200, response.content
    sqls = [q["sql"] for q in ctx.captured_queries]
    first = {
        name: next(i for i, sql in enumerate(sqls) if match(sql))
        for name, match in (
            ("pass", _row_lock_on("events_seriespass")),
            ("tier", _row_lock_on("events_tickettier")),
            ("event_write", lambda sql: sql.startswith('UPDATE "events_event"')),
        )
    }
    assert first["pass"] < first["tier"] < first["event_write"], first
    assert SeriesPassTierLink.objects.filter(event=event, tier=tier).exists()
