"""Tests for series pass extension: Celery materialization of newly-linked events (Task 10)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    Payment,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service import series_pass_service
from events.service.series_pass_service import TierLinkInput
from events.tasks.series_pass import materialize_series_pass_holders

pytestmark = pytest.mark.django_db


def _make_event(organization: Organization, event_series: EventSeries, name: str, slug: str) -> Event:
    return Event.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


def _make_tier(event: Event, name: str) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def new_events(organization: Organization, event_series: EventSeries) -> list[Event]:
    """Two events added to the series after the pass already had holders."""
    return [_make_event(organization, event_series, f"New Event {i}", f"new-event-{i}") for i in range(1, 3)]


@pytest.fixture
def new_tiers(new_events: list[Event]) -> list[TicketTier]:
    return [_make_tier(event, f"Tier for {event.name}") for event in new_events]


@pytest.fixture
def new_links(
    series_pass: SeriesPass, new_events: list[Event], new_tiers: list[TicketTier]
) -> list[SeriesPassTierLink]:
    return [
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
        for event, tier in zip(new_events, new_tiers, strict=True)
    ]


@pytest.fixture
def new_event_ids(new_links: list[SeriesPassTierLink]) -> list[str]:
    return [str(link.event_id) for link in new_links]


@pytest.fixture
def active_holder(series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=revel_user,
        price_paid=Decimal("36.00"),
        status=HeldSeriesPass.Status.ACTIVE,
    )


@pytest.fixture
def other_active_holder(series_pass: SeriesPass, revel_user_factory: RevelUserFactory) -> HeldSeriesPass:
    user = revel_user_factory(username="holder2@example.com", email="holder2@example.com")
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=user,
        price_paid=Decimal("36.00"),
        status=HeldSeriesPass.Status.ACTIVE,
    )


@pytest.fixture
def pending_holder(series_pass: SeriesPass, revel_user_factory: RevelUserFactory) -> HeldSeriesPass:
    user = revel_user_factory(username="pending@example.com", email="pending@example.com")
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=user,
        price_paid=Decimal("36.00"),
        status=HeldSeriesPass.Status.PENDING,
    )


@pytest.fixture
def cancelled_holder(series_pass: SeriesPass, revel_user_factory: RevelUserFactory) -> HeldSeriesPass:
    user = revel_user_factory(username="cancelled@example.com", email="cancelled@example.com")
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=user,
        price_paid=Decimal("36.00"),
        status=HeldSeriesPass.Status.CANCELLED,
    )


class TestMaterializeSeriesPassHolders:
    def test_active_holders_gain_active_tickets_for_new_events(
        self,
        series_pass: SeriesPass,
        new_events: list[Event],
        new_event_ids: list[str],
        active_holder: HeldSeriesPass,
        other_active_holder: HeldSeriesPass,
    ) -> None:
        """Every ACTIVE holder gets one ACTIVE ticket per newly-linked event, no Payment rows."""
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        for holder in (active_holder, other_active_holder):
            tickets = Ticket.objects.filter(held_pass=holder, event_id__in=[e.id for e in new_events])
            assert tickets.count() == 2
            assert {ticket.event_id for ticket in tickets} == {e.id for e in new_events}
            assert all(ticket.status == Ticket.TicketStatus.ACTIVE for ticket in tickets)
            assert not Payment.objects.filter(ticket__in=tickets).exists()

    def test_tier_quantity_sold_incremented_per_materialized_ticket(
        self,
        series_pass: SeriesPass,
        new_tiers: list[TicketTier],
        new_event_ids: list[str],
        active_holder: HeldSeriesPass,
        other_active_holder: HeldSeriesPass,
    ) -> None:
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        for tier in new_tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 2  # two ACTIVE holders

    def test_full_tier_is_skipped_for_all_holders_other_event_still_granted(
        self,
        series_pass: SeriesPass,
        new_events: list[Event],
        new_tiers: list[TicketTier],
        new_event_ids: list[str],
        active_holder: HeldSeriesPass,
        other_active_holder: HeldSeriesPass,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A tier already at capacity is skipped for every holder; the other new event still grants."""
        full_tier = new_tiers[0]
        full_tier.total_quantity = 1
        full_tier.quantity_sold = 1
        full_tier.save(update_fields=["total_quantity", "quantity_sold"])

        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            with caplog.at_level("INFO", logger="events.tasks.series_pass"):
                materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        full_event = new_events[0]
        open_event = new_events[1]
        assert not Ticket.objects.filter(event=full_event).exists()
        assert Ticket.objects.filter(event=open_event, held_pass=active_holder).exists()
        assert Ticket.objects.filter(event=open_event, held_pass=other_active_holder).exists()

        full_tier.refresh_from_db()
        assert full_tier.quantity_sold == 1  # unchanged — nobody was granted

        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("event") == "series_pass_extension_done"
            and record.msg.get("skipped") == 2
            for record in caplog.records
        )

    def test_rerun_is_idempotent(
        self,
        series_pass: SeriesPass,
        new_event_ids: list[str],
        active_holder: HeldSeriesPass,
        other_active_holder: HeldSeriesPass,
    ) -> None:
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), new_event_ids)
            first_run_count = Ticket.objects.count()
            materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        assert Ticket.objects.count() == first_run_count

    def test_pending_holder_gets_nothing(
        self,
        series_pass: SeriesPass,
        new_event_ids: list[str],
        pending_holder: HeldSeriesPass,
    ) -> None:
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        assert not Ticket.objects.filter(held_pass=pending_holder).exists()

    def test_cancelled_holder_gets_nothing(
        self,
        series_pass: SeriesPass,
        new_event_ids: list[str],
        cancelled_holder: HeldSeriesPass,
    ) -> None:
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        assert not Ticket.objects.filter(held_pass=cancelled_holder).exists()

    def test_holder_reread_takes_row_lock(
        self,
        series_pass: SeriesPass,
        new_event_ids: list[str],
        active_holder: HeldSeriesPass,
    ) -> None:
        """The per-holder re-read must lock the pass row (SELECT ... FOR UPDATE OF):
        without it a concurrent cancel_held_pass can commit CANCELLED between the
        ACTIVE re-check and bulk_create, leaving a cancelled pass with a live ticket."""
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            with CaptureQueriesContext(connection) as ctx:
                materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        table = HeldSeriesPass._meta.db_table
        locking_rereads = [
            query["sql"]
            for query in ctx.captured_queries
            if f'FROM "{table}"' in query["sql"] and "FOR UPDATE OF" in query["sql"]
        ]
        assert locking_rereads, "holder re-read must take a row lock on the held pass"

    def test_each_extended_holder_notified_with_created_event_ids(
        self,
        series_pass: SeriesPass,
        new_events: list[Event],
        new_event_ids: list[str],
        active_holder: HeldSeriesPass,
        other_active_holder: HeldSeriesPass,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("notifications.signals.series_pass.send_series_pass_extended") as mock_notify:
            with django_capture_on_commit_callbacks(execute=True):
                materialize_series_pass_holders(str(series_pass.id), new_event_ids)

        assert mock_notify.call_count == 2
        notified_calls = {call.args[0]: set(call.args[1]) for call in mock_notify.call_args_list}
        expected_event_ids = {e.id for e in new_events}
        assert notified_calls == {
            active_holder.id: expected_event_ids,
            other_active_holder.id: expected_event_ids,
        }


class TestAddTierLinksDispatch:
    @pytest.fixture
    def series_pass_payload_links(self, event: Event, ticket_tier: TicketTier) -> list[TierLinkInput]:
        return [{"event_id": event.id, "tier_id": ticket_tier.id}]

    def test_materialize_true_dispatches_task_on_commit(
        self,
        series_pass: SeriesPass,
        series_pass_payload_links: list[TierLinkInput],
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("events.service.series_pass_service.materialize_series_pass_holders.delay") as mock_delay:
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.add_tier_links(series_pass, series_pass_payload_links, materialize=True)

        mock_delay.assert_called_once()

    def test_materialize_false_does_not_dispatch(
        self,
        series_pass: SeriesPass,
        series_pass_payload_links: list[TierLinkInput],
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("events.service.series_pass_service.materialize_series_pass_holders.delay") as mock_delay:
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.add_tier_links(series_pass, series_pass_payload_links, materialize=False)

        mock_delay.assert_not_called()
