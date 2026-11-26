"""Series pass writers check the covered events under the event locks (#1496); the reconcile sweep (#1536)."""

import contextlib
import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import SeriesPassNotPurchasableError
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tasks.series_pass import materialize_series_pass_holders, reconcile_series_pass_tickets
from events.tests.test_series_pass.covered_events import CANCELLED, OPEN
from events.tests.test_series_pass.covered_events import cancel as _cancel
from events.tests.test_series_pass.covered_events import cover as _cover

pytestmark = pytest.mark.django_db


def _status_change_before_the_event_lock(event: Event, status: Event.EventStatus) -> t.Any:
    """Patch the event lock helper to change ``event``'s status first: a race after the lock-free reads (#1496)."""
    from events.service import series_pass_event_checks

    real = series_pass_event_checks.lock_events_and_find_full

    def change_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
        Event.objects.filter(pk=event.pk).update(status=status)
        return real(*args, **kwargs)

    return patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=change_then_lock)


@contextlib.contextmanager
def _refused_before_any_write(match: str | None = None) -> t.Iterator[None]:
    """Expect the 409 before the held pass is created.

    A row check after the call cannot fail: ``purchase`` is atomic, so the error rolls every write back.
    """
    with (
        patch.object(SeriesPassPurchaseService, "_create_held_pass", autospec=True) as create,
        pytest.raises(SeriesPassNotPurchasableError, match=match),
    ):
        yield
    create.assert_not_called()


class TestStatusChangeBeforeTheEventLock:
    """The status is checked again under the event lock, the lock that ``update_status`` takes first (#1496)."""

    def test_purchase_refuses_when_a_covered_event_is_cancelled(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        with (
            _status_change_before_the_event_lock(three_upcoming[1], CANCELLED),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_a_cancelled_covered_event_reopens(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        _cancel(three_upcoming[0])

        with (
            _status_change_before_the_event_lock(three_upcoming[0], OPEN),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

        # The lock-free quote gave a discount step for the cancelled event; the locked one does not.

    def test_purchase_refuses_when_an_open_covered_event_closes(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """The lock-free status gate saw OPEN; under the lock the event is CLOSED. The price is the same."""
        with (
            _status_change_before_the_event_lock(three_upcoming[1], Event.EventStatus.CLOSED),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_an_event_closed_after_the_status_gate(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """The change lands after the lock-free gate and before the locked re-read of the links."""
        real_gate = SeriesPassPurchaseService._assert_covered_events_eligible

        def gate_then_close(service: SeriesPassPurchaseService, links: list[SeriesPassTierLink]) -> None:
            real_gate(service, links)
            Event.objects.filter(pk=three_upcoming[1].pk).update(status=Event.EventStatus.CLOSED)

        with (
            patch.object(SeriesPassPurchaseService, "_assert_covered_events_eligible", gate_then_close),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_owner_is_not_refused_when_a_covered_event_closes(
        self, series_pass: SeriesPass, three_upcoming: list[Event], organization: Organization
    ) -> None:
        """Owners and staff are exempt from the status gate, also under the lock."""
        with _status_change_before_the_event_lock(three_upcoming[1], Event.EventStatus.CLOSED):
            held_pass = SeriesPassPurchaseService(series_pass, organization.owner).purchase()

        assert isinstance(held_pass, HeldSeriesPass)
        assert Ticket.objects.filter(held_pass=held_pass).count() == 3

    def test_purchase_gives_the_quote_reason_when_sales_stop(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        real_quote = series_pass_service.get_quote
        calls: list[int] = []

        def quote_then_stop_sales(*args: t.Any, **kwargs: t.Any) -> t.Any:
            quote = real_quote(*args, **kwargs)
            if not calls:
                SeriesPass.objects.filter(pk=series_pass.pk).update(is_active=False)
            calls.append(1)
            return quote

        with (
            patch.object(series_pass_service, "get_quote", side_effect=quote_then_stop_sales),
            _refused_before_any_write("This pass is not on sale."),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_the_payment_method_changes(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        real_quote = series_pass_service.get_quote
        calls: list[int] = []

        def quote_then_go_offline(*args: t.Any, **kwargs: t.Any) -> t.Any:
            quote = real_quote(*args, **kwargs)
            if not calls:
                SeriesPass.objects.filter(pk=series_pass.pk).update(payment_method=TicketTier.PaymentMethod.OFFLINE)
            calls.append(1)
            return quote

        with (
            patch.object(series_pass_service, "get_quote", side_effect=quote_then_go_offline),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_a_covered_event_is_deleted_before_the_event_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        from events.service import series_pass_event_checks

        real = series_pass_event_checks.lock_events_and_find_full

        def delete_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
            Event.objects.filter(pk=three_upcoming[1].pk).delete()
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=delete_then_lock),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_a_covered_event_is_deleted_before_the_tier_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """Before, the missing tier raised KeyError (500)."""
        from events.compliance import enforcement

        real = enforcement.assert_sale_allowed

        def sale_check_then_delete(*args: t.Any, **kwargs: t.Any) -> t.Any:
            result = real(*args, **kwargs)
            Event.objects.filter(pk=three_upcoming[1].pk).delete()
            return result

        with (
            patch.object(enforcement, "assert_sale_allowed", side_effect=sale_check_then_delete),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_a_covered_event_moves_into_the_past(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """No discount step, so only the remaining count can see it."""
        series_pass.pro_rata_discount = Decimal("0.00")
        series_pass.save(update_fields=["pro_rata_discount"])
        from events.service import series_pass_event_checks

        real = series_pass_event_checks.lock_events_and_find_full

        def move_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
            Event.objects.filter(pk=three_upcoming[0].pk).update(start=timezone.now() - timedelta(hours=1))
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=move_then_lock),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    @pytest.mark.parametrize(
        "change",
        [
            {"purchasable_by": TicketTier.PurchasableBy.MEMBERS},
            {"currency": "USD"},
        ],
        ids=["buyer_rule", "currency"],
    )
    def test_purchase_refuses_when_a_pass_field_changes(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser, change: dict[str, str]
    ) -> None:
        real_quote = series_pass_service.get_quote
        calls: list[int] = []

        def quote_then_change(*args: t.Any, **kwargs: t.Any) -> t.Any:
            quote = real_quote(*args, **kwargs)
            if not calls:
                SeriesPass.objects.filter(pk=series_pass.pk).update(**change)
            calls.append(1)
            return quote

        with (
            patch.object(series_pass_service, "get_quote", side_effect=quote_then_change),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_the_pass_moves_to_another_series(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """The checks before the pass lock used the old series and organization (#1577)."""
        other_org = Organization.objects.create(name="Other", slug="other-1577", owner=organization_owner_user)
        other_series = EventSeries.objects.create(organization=other_org, name="Other", slug="other-1577")
        real_quote = series_pass_service.get_quote
        calls: list[int] = []

        def quote_then_move(*args: t.Any, **kwargs: t.Any) -> t.Any:
            quote = real_quote(*args, **kwargs)
            if not calls:
                SeriesPass.objects.filter(pk=series_pass.pk).update(event_series=other_series)
            calls.append(1)
            return quote

        with (
            patch.object(series_pass_service, "get_quote", side_effect=quote_then_move),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_a_swap_that_keeps_the_price_and_the_count(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """One covered event reopens and another is cancelled: same price, same count, other events."""
        _cancel(three_upcoming[0])
        from events.service import series_pass_event_checks

        real = series_pass_event_checks.lock_events_and_find_full

        def swap_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
            Event.objects.filter(pk=three_upcoming[0].pk).update(status=OPEN)
            _cancel(three_upcoming[1])
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=swap_then_lock),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_covered_events_swap_start_times(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """One event moves into the past and a past one into the future: same price, same count."""
        past, _ = _cover(series_pass, "past", days=-1)
        from events.service import series_pass_event_checks

        real = series_pass_event_checks.lock_events_and_find_full

        def swap_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
            Event.objects.filter(pk=three_upcoming[0].pk).update(start=timezone.now() - timedelta(hours=1))
            Event.objects.filter(pk=past.pk).update(start=timezone.now() + timedelta(days=20))
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=swap_then_lock),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_an_event_that_starts_before_the_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """The quote under the locks takes a new ``now``: an event that started meanwhile changes its set.

        No discount step, so the price stays the same and only the event set can refuse it.
        """
        series_pass.pro_rata_discount = Decimal("0.00")
        series_pass.save(update_fields=["pro_rata_discount"])
        first = timezone.now()
        Event.objects.filter(pk=three_upcoming[0].pk).update(start=first + timedelta(minutes=30))
        clock = MagicMock()
        clock.now.side_effect = lambda: first if clock.now.call_count == 1 else first + timedelta(hours=1)

        with (
            patch("events.service.series_pass_purchase.timezone", clock),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_a_changed_event_answers_409_not_sold_out(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """A full tier of an event cancelled after the links read: the 409 checks run before the 429."""
        TicketTier.objects.filter(event=three_upcoming[1]).update(total_quantity=0)
        from events.compliance import enforcement

        real = enforcement.assert_sale_allowed

        def sale_check_then_cancel(*args: t.Any, **kwargs: t.Any) -> t.Any:
            result = real(*args, **kwargs)
            _cancel(three_upcoming[1])
            return result

        with (
            patch.object(enforcement, "assert_sale_allowed", side_effect=sale_check_then_cancel),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_a_full_tier_without_a_change_is_still_sold_out(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        TicketTier.objects.filter(event=three_upcoming[1]).update(total_quantity=0)

        with pytest.raises(HttpError) as exc_info:
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

        assert exc_info.value.status_code == 429

    def test_purchase_refuses_a_cancel_that_keeps_the_price(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """No discount step: only the status comparison can refuse it."""
        series_pass.pro_rata_discount = Decimal("0.00")
        series_pass.save(update_fields=["pro_rata_discount"])

        with (
            _status_change_before_the_event_lock(three_upcoming[1], CANCELLED),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_the_organizer_changes_the_price(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """A price change between the quote and the pass lock: the quote under the lock reads the locked row."""
        real_quote = series_pass_service.get_quote
        calls: list[int] = []

        def quote_then_reprice(*args: t.Any, **kwargs: t.Any) -> t.Any:
            quote = real_quote(*args, **kwargs)
            if not calls:
                SeriesPass.objects.filter(pk=series_pass.pk).update(price=Decimal("50.00"))
            calls.append(1)
            return quote

        with (
            patch.object(series_pass_service, "get_quote", side_effect=quote_then_reprice),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

    def test_purchase_refuses_when_the_price_changed_after_the_quote(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        """A cancel between the quote and the covered-events read: the statuses agree, the price does not."""
        real_quote = series_pass_service.get_quote
        calls: list[int] = []

        def quote_then_cancel(*args: t.Any, **kwargs: t.Any) -> t.Any:
            quote = real_quote(*args, **kwargs)
            if not calls:
                _cancel(three_upcoming[2])
            calls.append(1)
            return quote

        with (
            patch.object(series_pass_service, "get_quote", side_effect=quote_then_cancel),
            _refused_before_any_write("The events of this pass changed"),
        ):
            SeriesPassPurchaseService(series_pass, revel_user).purchase()

        assert len(calls) == 2

    def test_purchase_without_a_status_change_still_succeeds(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        _cancel(three_upcoming[0])

        with _status_change_before_the_event_lock(three_upcoming[0], CANCELLED):
            held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()

        assert isinstance(held_pass, HeldSeriesPass)
        assert held_pass.price_paid == Decimal("30.00")
        ticketed = set(Ticket.objects.filter(held_pass=held_pass).values_list("event_id", flat=True))
        assert ticketed == {three_upcoming[1].pk, three_upcoming[2].pk}

    def test_extension_skips_an_event_cancelled_before_the_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        new_event, new_tier = _cover(series_pass, "new", days=10)

        with (
            _status_change_before_the_event_lock(new_event, CANCELLED),
            patch("notifications.signals.series_pass.send_series_pass_extended"),
        ):
            materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])

        assert not Ticket.objects.filter(held_pass=held_pass, event=new_event).exists()
        new_tier.refresh_from_db()
        assert new_tier.quantity_sold == 0

    def test_extension_skips_an_event_that_starts_before_the_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        new_event, new_tier = _cover(series_pass, "new", days=10)
        from events.service import series_pass_event_checks

        real = series_pass_event_checks.lock_events_and_find_full

        def start_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
            Event.objects.filter(pk=new_event.pk).update(start=timezone.now() - timedelta(minutes=1))
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=start_then_lock),
            patch("notifications.signals.series_pass.send_series_pass_extended"),
        ):
            materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])

        assert not Ticket.objects.filter(held_pass=held_pass, event=new_event).exists()
        new_tier.refresh_from_db()
        assert new_tier.quantity_sold == 0

    def test_backfill_skips_an_event_that_starts_before_the_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
        series_pass.save(update_fields=["payment_method"])
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        late, _ = _cover(series_pass, "late", days=10)
        from events.service import series_pass_event_checks

        real = series_pass_event_checks.lock_events_and_find_full

        def start_then_lock(*args: t.Any, **kwargs: t.Any) -> t.Any:
            Event.objects.filter(pk=late.pk).update(start=timezone.now() - timedelta(minutes=1))
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, "lock_events_and_find_full", side_effect=start_then_lock),
            patch("events.service.series_pass_service.send_series_pass_purchased"),
        ):
            series_pass_service.confirm_held_pass_payment(held_pass)

        assert not Ticket.objects.filter(held_pass=held_pass, event=late).exists()

    @pytest.mark.parametrize("before", ["tier_lock", "event_lock"])
    def test_extension_skips_an_event_deleted_after_the_snapshot(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser, before: str
    ) -> None:
        """Before, a deleted tier raised KeyError and stopped the task.

        ``event_lock`` deletes in the same transaction, after the tier lock: it tests the defensive branch
        for an event missing at the event lock. A delete from another connection waits for the tier lock
        instead (the delete-service lock order, #1538).
        """
        from events.service import series_pass_event_checks

        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        doomed, _ = _cover(series_pass, "doomed", days=10)
        kept, _ = _cover(series_pass, "kept", days=11)
        name = "is_blocked_by_blacklist" if before == "tier_lock" else "lock_events_and_find_full"
        real = getattr(series_pass_event_checks, name)

        def delete_then_continue(*args: t.Any, **kwargs: t.Any) -> t.Any:
            if before == "tier_lock":  # Right after the blacklist check, which reads the event.
                result = real(*args, **kwargs)
                Event.objects.filter(pk=doomed.pk).delete()
                return result
            Event.objects.filter(pk=doomed.pk).delete()
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, name, side_effect=delete_then_continue),
            patch("notifications.signals.series_pass.send_series_pass_extended"),
        ):
            materialize_series_pass_holders(str(series_pass.id), [str(doomed.id), str(kept.id)])

        assert Ticket.objects.filter(held_pass=held_pass, event=kept).exists()
        assert not Ticket.objects.filter(event_id=doomed.pk).exists()

    @pytest.mark.parametrize("before", ["tier_lock", "event_lock"])
    def test_backfill_skips_an_event_deleted_after_the_links_query(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser, before: str
    ) -> None:
        from events.service import series_pass_event_checks

        series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
        series_pass.save(update_fields=["payment_method"])
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        doomed, _ = _cover(series_pass, "doomed", days=10)
        kept, _ = _cover(series_pass, "kept", days=11)
        name = "is_blocked_by_blacklist" if before == "tier_lock" else "lock_events_and_find_full"
        real = getattr(series_pass_event_checks, name)

        def delete_then_continue(*args: t.Any, **kwargs: t.Any) -> t.Any:
            if before == "tier_lock":  # Right after the blacklist check, which reads the event.
                result = real(*args, **kwargs)
                Event.objects.filter(pk=doomed.pk).delete()
                return result
            Event.objects.filter(pk=doomed.pk).delete()
            return real(*args, **kwargs)

        with (
            patch.object(series_pass_event_checks, name, side_effect=delete_then_continue),
            patch("events.service.series_pass_service.send_series_pass_purchased"),
        ):
            series_pass_service.confirm_held_pass_payment(held_pass)

        assert Ticket.objects.filter(held_pass=held_pass, event=kept).exists()
        assert not Ticket.objects.filter(event_id=doomed.pk).exists()

    def test_backfill_skips_an_event_cancelled_before_the_lock(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
        series_pass.save(update_fields=["payment_method"])
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        late, late_tier = _cover(series_pass, "late", days=10)

        with (
            _status_change_before_the_event_lock(late, CANCELLED),
            patch("events.service.series_pass_service.send_series_pass_purchased"),
        ):
            series_pass_service.confirm_held_pass_payment(held_pass)

        assert not Ticket.objects.filter(held_pass=held_pass, event=late).exists()
        late_tier.refresh_from_db()
        assert late_tier.quantity_sold == 0


class TestReconcileSweep:
    """``reconcile_series_pass_tickets`` issues what a failed materialization dispatch missed (#1536)."""

    @pytest.fixture
    def held_pass(self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser) -> HeldSeriesPass:
        held = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held, HeldSeriesPass)
        assert held.status == HeldSeriesPass.HeldSeriesPassStatus.ACTIVE
        return held

    @pytest.fixture(autouse=True)
    def _no_extension_email(self) -> t.Iterator[None]:
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            yield

    def test_issues_the_missing_ticket_once(self, series_pass: SeriesPass, held_pass: HeldSeriesPass) -> None:
        # Linked without running the extension task: the state a failed enqueue leaves.
        missed, missed_tier = _cover(series_pass, "missed", days=10)

        assert reconcile_series_pass_tickets() == 1
        assert reconcile_series_pass_tickets() == 0

        ticket = Ticket.objects.get(held_pass=held_pass, event=missed)
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.price_paid == Decimal("0.00")
        missed_tier.refresh_from_db()
        assert missed_tier.quantity_sold == 1

    def test_never_reissues_an_organizer_cancelled_ticket(
        self, held_pass: HeldSeriesPass, three_upcoming: list[Event]
    ) -> None:
        Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).update(status=Ticket.TicketStatus.CANCELLED)

        assert reconcile_series_pass_tickets() == 0
        assert Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).count() == 1

    def test_uncancel_dispatch_never_reissues_an_organizer_cancelled_ticket(
        self, series_pass: SeriesPass, held_pass: HeldSeriesPass, three_upcoming: list[Event]
    ) -> None:
        """The extension reads the holder's tickets of any status under the pass lock (also the sweep's race)."""
        Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).update(status=Ticket.TicketStatus.CANCELLED)

        materialize_series_pass_holders(str(series_pass.id), [str(three_upcoming[0].id)])

        assert Ticket.objects.filter(held_pass=held_pass, event=three_upcoming[0]).count() == 1

    def test_skips_cancelled_and_past_events(self, series_pass: SeriesPass, held_pass: HeldSeriesPass) -> None:
        cancelled, _ = _cover(series_pass, "cancelled", days=10, status=CANCELLED)
        past, _ = _cover(series_pass, "past", days=-1)

        assert reconcile_series_pass_tickets() == 0
        assert not Ticket.objects.filter(held_pass=held_pass, event__in=[cancelled, past]).exists()

    def test_skips_a_pass_that_is_not_active(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
        series_pass.save(update_fields=["payment_method"])
        pending = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(pending, HeldSeriesPass)
        missed, _ = _cover(series_pass, "missed", days=10)

        assert reconcile_series_pass_tickets() == 0
        assert not Ticket.objects.filter(held_pass=pending, event=missed).exists()

    def test_a_full_tier_is_tried_again_when_a_seat_frees(
        self, series_pass: SeriesPass, held_pass: HeldSeriesPass
    ) -> None:
        missed, missed_tier = _cover(series_pass, "missed", days=10)
        TicketTier.objects.filter(pk=missed_tier.pk).update(total_quantity=0)

        assert reconcile_series_pass_tickets() == 0

        TicketTier.objects.filter(pk=missed_tier.pk).update(total_quantity=1)
        assert reconcile_series_pass_tickets() == 1
        assert Ticket.objects.filter(held_pass=held_pass, event=missed).exists()

    def test_beat_row_is_registered(self) -> None:
        from django_celery_beat.models import PeriodicTask

        row = PeriodicTask.objects.get(task="events.reconcile_series_pass_tickets")
        assert row.enabled is True
        assert row.interval is not None and (row.interval.every, row.interval.period) == (15, "minutes")
