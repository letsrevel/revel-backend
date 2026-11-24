"""A CANCELLED covered event does not stop pass sales (#1494).

It gives one pro-rata discount step (like an event that has started, counted once), it is not
remaining, and no flow issues a ticket for it. DRAFT and other non-OPEN upcoming events still block.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    HeldSeriesPass,
    Organization,
    OrganizationQuestionnaire,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.schema.series_pass import HeldSeriesPassSchema
from events.service import event_update_service, series_pass_file_service, series_pass_service
from events.service.check_in_service import resolve_check_in_ticket_id
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.series_pass_questionnaires import check_series_questionnaires
from events.service.series_pass_service import get_quote
from events.tasks.series_pass import materialize_series_pass_holders
from questionnaires.models import Questionnaire
from wallet.resolution import resolve_series_window

pytestmark = pytest.mark.django_db

OPEN = Event.EventStatus.OPEN
CANCELLED = Event.EventStatus.CANCELLED


def _cover(series_pass: SeriesPass, slug: str, days: int, status: Event.EventStatus = OPEN) -> tuple[Event, TicketTier]:
    """Create a covered event and its FREE tier. The link is created directly, past the coverage gate."""
    series = series_pass.event_series
    event = Event.objects.create(
        organization=series.organization,
        name=f"Event {slug}",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=days),
        status=status,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=event,
        name=f"Tier {slug}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return event, tier


@pytest.fixture
def three_upcoming(series_pass: SeriesPass) -> list[Event]:
    """Three upcoming OPEN covered events. ``series_pass``: price 36.00, discount 6.00, FREE."""
    return [_cover(series_pass, f"up-{i}", days=i + 1)[0] for i in range(3)]


def _cancel(event: Event) -> None:
    Event.objects.filter(pk=event.pk).update(status=CANCELLED)


class TestQuote:
    def test_upcoming_cancelled_gives_one_discount_step_and_is_not_remaining(
        self, series_pass: SeriesPass, three_upcoming: list[Event]
    ) -> None:
        _cancel(three_upcoming[1])

        quote = get_quote(series_pass)

        assert quote.purchasable is True
        assert quote.reason is None
        assert quote.remaining_events == 2
        assert quote.passed_events == 1
        assert quote.price == Decimal("30.00")

    def test_past_cancelled_event_is_counted_once(self, series_pass: SeriesPass, three_upcoming: list[Event]) -> None:
        past, _ = _cover(series_pass, "past", days=-3, status=CANCELLED)
        _cancel(three_upcoming[0])

        quote = get_quote(series_pass)

        # One step for the past cancelled event (not two), one for the upcoming cancelled event.
        assert quote.passed_events == 2
        assert quote.remaining_events == 2
        assert quote.price == Decimal("24.00")
        assert past.start < timezone.now()

    def test_draft_upcoming_event_still_blocks(self, series_pass: SeriesPass, three_upcoming: list[Event]) -> None:
        _cancel(three_upcoming[0])
        Event.objects.filter(pk=three_upcoming[1].pk).update(status=Event.EventStatus.DRAFT)

        quote = get_quote(series_pass)

        assert quote.purchasable is False
        assert quote.reason == "Event is not open."

    def test_only_one_non_cancelled_upcoming_event_is_not_purchasable(
        self, series_pass: SeriesPass, three_upcoming: list[Event]
    ) -> None:
        _cancel(three_upcoming[0])
        _cancel(three_upcoming[1])

        quote = get_quote(series_pass)

        assert quote.purchasable is False
        assert quote.remaining_events == 1
        assert quote.reason == "Not enough remaining events; buy a regular ticket instead."

    def test_price_floors_at_zero(self, series_pass: SeriesPass, three_upcoming: list[Event]) -> None:
        series_pass.pro_rata_discount = Decimal("40.00")
        series_pass.save(update_fields=["pro_rata_discount"])
        _cover(series_pass, "up-3", days=4)
        _cancel(three_upcoming[0])

        quote = get_quote(series_pass)

        assert quote.purchasable is True
        assert quote.price == Decimal("0.00")


class TestPurchase:
    def test_purchase_issues_no_ticket_for_cancelled_event(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        _cancel(three_upcoming[1])

        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()

        assert isinstance(held_pass, HeldSeriesPass)
        assert held_pass.price_paid == Decimal("30.00")
        ticketed = set(Ticket.objects.filter(held_pass=held_pass).values_list("event_id", flat=True))
        assert ticketed == {three_upcoming[0].pk, three_upcoming[2].pk}
        cancelled_tier = SeriesPassTierLink.objects.get(series_pass=series_pass, event=three_upcoming[1]).tier
        assert cancelled_tier.quantity_sold == 0

    def test_questionnaire_of_cancelled_event_is_not_required(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        revel_user: RevelUser,
        organization: Organization,
        questionnaire: Questionnaire,
    ) -> None:
        oq = OrganizationQuestionnaire.objects.create(
            organization=organization, questionnaire=questionnaire, requires_evaluation=False
        )
        oq.events.add(three_upcoming[1])
        # Control: while the event is OPEN, the questionnaire blocks the purchase.
        assert check_series_questionnaires(series_pass, revel_user, owner_or_staff=False) is not None
        _cancel(three_upcoming[1])
        assert check_series_questionnaires(series_pass, revel_user, owner_or_staff=False) is None

        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()

        assert isinstance(held_pass, HeldSeriesPass)


def test_offline_confirm_backfill_skips_cancelled_event(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
) -> None:
    series_pass.payment_method = TicketTier.PaymentMethod.OFFLINE
    series_pass.save(update_fields=["payment_method"])
    held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    # Linked while the pass is PENDING: the confirm backfills the open one only.
    late_open, _ = _cover(series_pass, "late-open", days=10)
    late_cancelled, _ = _cover(series_pass, "late-cancelled", days=11, status=CANCELLED)

    with patch("events.service.series_pass_service.send_series_pass_purchased"):
        series_pass_service.confirm_held_pass_payment(held_pass)

    ticketed = set(Ticket.objects.filter(held_pass=held_pass).values_list("event_id", flat=True))
    assert late_open.pk in ticketed
    assert late_cancelled.pk not in ticketed


def test_extension_skips_cancelled_event(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
) -> None:
    held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    new_open, _ = _cover(series_pass, "new-open", days=10)
    new_cancelled, _ = _cover(series_pass, "new-cancelled", days=11, status=CANCELLED)

    with patch("notifications.signals.series_pass.send_series_pass_extended"):
        materialize_series_pass_holders(str(series_pass.id), [str(new_open.id), str(new_cancelled.id)])

    assert Ticket.objects.filter(held_pass=held_pass, event=new_open).exists()
    assert not Ticket.objects.filter(held_pass=held_pass, event=new_cancelled).exists()


class TestHolderViews:
    @pytest.fixture
    def held_pass(self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser) -> HeldSeriesPass:
        held = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held, HeldSeriesPass)
        _cancel(three_upcoming[0])
        return HeldSeriesPass.objects.select_related("series_pass__event_series__organization", "user").get(pk=held.pk)

    def test_remaining_event_count_leaves_out_cancelled(self, held_pass: HeldSeriesPass) -> None:
        assert HeldSeriesPassSchema.resolve_remaining_event_count(held_pass) == 2
        assert HeldSeriesPassSchema.resolve_total_event_count(held_pass) == 3

    @patch("qrcode.QRCode")
    @patch("weasyprint.HTML")
    @patch("events.utils.render_to_string")
    def test_pdf_lists_no_cancelled_event(
        self,
        mock_render: MagicMock,
        mock_html: MagicMock,
        mock_qr: MagicMock,
        held_pass: HeldSeriesPass,
        three_upcoming: list[Event],
    ) -> None:
        mock_html.return_value.write_pdf.return_value = b"%PDF-fake"
        mock_render.return_value = "<html></html>"
        from events.utils import create_series_pass_pdf

        create_series_pass_pdf(held_pass)

        context: dict[str, t.Any] = mock_render.call_args.kwargs["context"]
        names = [covered["name"] for covered in context["covered_events"]]
        assert names == [three_upcoming[1].name, three_upcoming[2].name]

    def test_wallet_representative_is_not_cancelled(
        self, held_pass: HeldSeriesPass, three_upcoming: list[Event]
    ) -> None:
        window = resolve_series_window(held_pass)

        assert window.representative_event is not None
        assert window.representative_event.pk == three_upcoming[1].pk

    def test_wallet_window_ends_with_last_non_cancelled_event(
        self, held_pass: HeldSeriesPass, three_upcoming: list[Event]
    ) -> None:
        _cancel(three_upcoming[2])
        last_live = Event.objects.get(pk=three_upcoming[1].pk)

        window = resolve_series_window(held_pass)

        assert window.end == last_live.end
        assert last_live.end < Event.objects.get(pk=three_upcoming[2].pk).end


def test_cancel_invalidates_cached_pass_files(
    series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
) -> None:
    """The cancel paths save with ``update_fields=["status", ...]``, so ``updated_at`` stays the same."""
    held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    before = series_pass_file_service.compute_content_hash(held_pass)
    event = three_upcoming[0]
    old_updated_at = event.updated_at

    event.status = CANCELLED
    event.save(update_fields=["status"])

    event.refresh_from_db()
    assert event.updated_at == old_updated_at
    assert series_pass_file_service.compute_content_hash(held_pass) != before


class TestSiblingFormatInvalidation:
    """PDF and pkpass share ``file_content_hash``: a new hash must not vouch for the other cached format."""

    @staticmethod
    def _fresh(held_pass: HeldSeriesPass) -> HeldSeriesPass:
        return HeldSeriesPass.objects.select_related("series_pass__event_series__organization", "user").get(
            pk=held_pass.pk
        )

    def test_pdf_regeneration_after_a_cancel_drops_the_stale_pkpass(
        self, series_pass: SeriesPass, three_upcoming: list[Event], revel_user: RevelUser
    ) -> None:
        held = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held, HeldSeriesPass)
        series_pass_file_service._persist_and_update(self._fresh(held), pdf_bytes=b"%PDF-old", pkpass_bytes=b"PK-old")
        _cancel(three_upcoming[0])
        generator = MagicMock()
        generator.generate_series_pass.return_value = b"PK-new"

        with patch("events.utils.create_series_pass_pdf", return_value=b"%PDF-new"):
            assert series_pass_file_service.get_or_generate_pass_pdf(self._fresh(held)) == b"%PDF-new"
        stale_dropped = self._fresh(held)
        assert not stale_dropped.pkpass_file
        with patch("events.service.series_pass_file_service.get_apple_pass_generator", return_value=generator):
            assert series_pass_file_service.get_or_generate_pass_pkpass(stale_dropped) == b"PK-new"

        generator.generate_series_pass.assert_called_once()


class TestUncancel:
    """Un-cancelling a covered event issues its ticket to the holders (owner decision on #1494)."""

    def test_holder_who_bought_while_cancelled_gets_the_ticket(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        cancelled = three_upcoming[1]
        _cancel(cancelled)
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        assert not Ticket.objects.filter(held_pass=held_pass, event=cancelled).exists()

        with (
            patch("notifications.signals.series_pass.send_series_pass_extended") as extended,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=cancelled.pk), OPEN)

        extended.assert_called_once_with(held_pass.id, [cancelled.id])
        ticket = Ticket.objects.get(held_pass=held_pass, event=cancelled)
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.price_paid == Decimal("0.00")
        assert resolve_check_in_ticket_id(cancelled, f"series:{held_pass.id}") == ticket.id
        held_pass.refresh_from_db()
        assert held_pass.price_paid == Decimal("30.00")  # The discount the buyer got stays.

    def test_holder_with_a_ticket_gets_no_second_one(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)
        event = three_upcoming[0]
        _cancel(event)  # Cancelled without refunds: the holder keeps the ticket.

        with (
            patch(
                "events.service.event_update_service.materialize_series_pass_holders",
                wraps=materialize_series_pass_holders,
            ) as task,
            patch("notifications.signals.series_pass.send_series_pass_extended") as extended,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=event.pk), OPEN)

        task.delay.assert_called_once()  # The task ran, and skipped the holder.
        assert Ticket.objects.filter(held_pass=held_pass, event=event).count() == 1
        extended.assert_not_called()

    def test_no_dispatch_for_an_event_no_pass_covers(
        self, event: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _cancel(event)

        with (
            patch("events.service.event_update_service.materialize_series_pass_holders") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=event.pk), OPEN)

        task.delay.assert_not_called()

    def test_dispatches_once_per_covering_pass(
        self, series_pass: SeriesPass, three_upcoming: list[Event], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        event = three_upcoming[0]
        _cancel(event)

        with (
            patch("events.service.event_update_service.materialize_series_pass_holders") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=event.pk), OPEN)

        task.delay.assert_called_once_with(str(series_pass.id), [str(event.id)])

    @pytest.mark.parametrize("target", [Event.EventStatus.CLOSED, Event.EventStatus.DRAFT])
    def test_dispatches_when_the_event_leaves_cancelled_for_any_status(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        target: Event.EventStatus,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """CANCELLED to CLOSED: the event still happens, so the holders need the ticket."""
        event = three_upcoming[0]
        _cancel(event)

        with (
            patch("events.service.event_update_service.materialize_series_pass_holders") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=event.pk), target)

        task.delay.assert_called_once_with(str(series_pass.id), [str(event.id)])

    def test_closed_event_ticket_is_issued_to_the_holder(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        cancelled = three_upcoming[1]
        _cancel(cancelled)
        held_pass = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(held_pass, HeldSeriesPass)

        with (
            patch("notifications.signals.series_pass.send_series_pass_extended"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=cancelled.pk), Event.EventStatus.CLOSED)

        assert Ticket.objects.get(held_pass=held_pass, event=cancelled).status == Ticket.TicketStatus.ACTIVE

    @pytest.mark.parametrize(
        ("start", "target"),
        [(OPEN, Event.EventStatus.CLOSED), (Event.EventStatus.DRAFT, OPEN), (OPEN, CANCELLED)],
    )
    def test_no_dispatch_when_the_event_does_not_leave_cancelled(
        self,
        series_pass: SeriesPass,
        three_upcoming: list[Event],
        start: Event.EventStatus,
        target: Event.EventStatus,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        event = three_upcoming[0]
        Event.objects.filter(pk=event.pk).update(status=start)

        with (
            patch("events.service.event_update_service.materialize_series_pass_holders") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(Event.objects.get(pk=event.pk), target)

        task.delay.assert_not_called()

    def test_no_dispatch_for_a_past_event(
        self, series_pass: SeriesPass, three_upcoming: list[Event], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        past, _ = _cover(series_pass, "past-reopen", days=-2, status=CANCELLED)

        with (
            patch("events.service.event_update_service.materialize_series_pass_holders") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_update_service.update_status(past, OPEN)

        task.delay.assert_not_called()
