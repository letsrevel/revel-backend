"""Tests for series pass activation on Stripe checkout.session.completed webhooks."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
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
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.stripe_webhooks import StripeEventHandler
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db


def _purchase_pending_pass(
    organization: Organization,
    event_series: EventSeries,
    user: RevelUser,
    session_id: str,
) -> HeldSeriesPass:
    """Drive the real ONLINE purchase flow (mocked Stripe) to a PENDING HeldSeriesPass."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name=f"Season Ticket {session_id}",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    now = timezone.now()
    for i in range(2):
        event = Event.objects.create(
            organization=organization,
            name=f"Future {session_id} {i}",
            slug=f"future-{session_id}-{i}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=now + timedelta(days=i + 1),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name=f"Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)

    mock_session = Mock()
    mock_session.id = session_id
    mock_session.url = f"https://checkout.stripe.com/pay/{session_id}"
    with patch("stripe.checkout.Session.create", return_value=mock_session):
        SeriesPassPurchaseService(series_pass, user).purchase()

    return HeldSeriesPass.objects.get(series_pass=series_pass, user=user)


def _completed_checkout_event(session_id: str) -> MagicMock:
    """Build a fake, iterable ``checkout.session.completed`` stripe.Event."""
    session_data = {"id": session_id, "payment_status": "paid", "payment_intent": f"pi_{session_id}"}
    event_data = {"type": "checkout.session.completed", "data": {"object": session_data}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = session_data
    return mock_event


class TestSeriesPassWebhookActivation:
    """checkout.session.completed flips a PENDING HeldSeriesPass to ACTIVE."""

    def test_completed_session_activates_pass_and_tickets(
        self,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        held_pass = _purchase_pending_pass(stripe_connected_organization, event_series, revel_user, "cs_pass_ok")
        payments = list(Payment.objects.filter(stripe_session_id="cs_pass_ok"))
        assert payments and all(p.status == Payment.PaymentStatus.PENDING for p in payments)

        event = _completed_checkout_event("cs_pass_ok")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(event).handle_checkout_session_completed(event)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.ACTIVE

        for payment in payments:
            payment.refresh_from_db()
            assert payment.status == Payment.PaymentStatus.SUCCEEDED

        for ticket in Ticket.objects.filter(held_pass=held_pass):
            assert ticket.status == Ticket.TicketStatus.ACTIVE

    def test_completed_session_does_not_resurrect_cancelled_ticket(
        self,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        """A late payment on a checkout whose ticket was cancelled in the meantime must
        keep the ticket CANCELLED (payment bookkeeping still runs)."""
        held_pass = _purchase_pending_pass(stripe_connected_organization, event_series, revel_user, "cs_pass_late")
        tickets = list(Ticket.objects.filter(held_pass=held_pass).order_by("created_at"))
        cancelled_ticket, other_ticket = tickets[0], tickets[1]
        Ticket.objects.filter(pk=cancelled_ticket.pk).update(status=Ticket.TicketStatus.CANCELLED)

        event = _completed_checkout_event("cs_pass_late")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(event).handle_checkout_session_completed(event)

        cancelled_ticket.refresh_from_db()
        assert cancelled_ticket.status == Ticket.TicketStatus.CANCELLED
        other_ticket.refresh_from_db()
        assert other_ticket.status == Ticket.TicketStatus.ACTIVE
        # Payment bookkeeping unchanged: both payments recorded as SUCCEEDED.
        for payment in Payment.objects.filter(stripe_session_id="cs_pass_late"):
            assert payment.status == Payment.PaymentStatus.SUCCEEDED

    def test_duplicate_delivery_is_idempotent(
        self,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        held_pass = _purchase_pending_pass(stripe_connected_organization, event_series, revel_user, "cs_pass_dup")

        event = _completed_checkout_event("cs_pass_dup")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(event).handle_checkout_session_completed(event)

        # Second delivery: fresh event object (Stripe redelivers independently), no errors.
        replay_event = _completed_checkout_event("cs_pass_dup")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(replay_event).handle_checkout_session_completed(replay_event)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.ACTIVE


class TestPerTicketConfirmationSuppression:
    """Series-pass tickets skip the per-ticket PAYMENT_CONFIRMATION notification."""

    @patch("notifications.signals.notification_requested.send")
    def test_pass_ticket_payment_suppresses_confirmation(
        self,
        mock_notification_signal: Mock,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        _purchase_pending_pass(stripe_connected_organization, event_series, revel_user, "cs_pass_notif")

        event = _completed_checkout_event("cs_pass_notif")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(event).handle_checkout_session_completed(event)

        confirmation_calls = [
            call
            for call in mock_notification_signal.call_args_list
            if call.kwargs["notification_type"] == NotificationType.PAYMENT_CONFIRMATION
        ]
        assert confirmation_calls == []

    @patch("notifications.signals.notification_requested.send")
    def test_regular_ticket_payment_still_sends_confirmation(
        self,
        mock_notification_signal: Mock,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        event = Event.objects.create(
            organization=stripe_connected_organization,
            name="Regular Event",
            slug="regular-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=timezone.now() + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Regular Tier",
            price=Decimal("15.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name=revel_user.get_display_name(),
        )
        Payment.objects.create(
            ticket=ticket,
            user=revel_user,
            stripe_session_id="cs_regular",
            amount=Decimal("15.00"),
            platform_fee=Decimal("1.00"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

        webhook_event = _completed_checkout_event("cs_regular")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(webhook_event).handle_checkout_session_completed(webhook_event)

        confirmation_calls = [
            call
            for call in mock_notification_signal.call_args_list
            if call.kwargs["notification_type"] == NotificationType.PAYMENT_CONFIRMATION
        ]
        assert len(confirmation_calls) == 1
        assert confirmation_calls[0].kwargs["user"] == revel_user


class TestPerTicketNotificationSuppression:
    """Series-pass tickets skip per-ticket TICKET_* notifications on activation (#644).

    ``handle_checkout_session_completed`` flips each pass ticket PENDING->ACTIVE via
    a per-instance ``.save()`` (not bulk_create), which would otherwise fan out a
    staff TICKET_CREATED and a holder "activated" TICKET_UPDATED per covered event.
    """

    @patch("notifications.signals.notification_requested.send")
    def test_pass_activation_sends_zero_ticket_notifications_and_one_purchased(
        self,
        mock_notification_signal: Mock,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        held_pass = _purchase_pending_pass(stripe_connected_organization, event_series, revel_user, "cs_pass_flood")
        assert Ticket.objects.filter(held_pass=held_pass).count() == 2  # sanity: 2-event pass

        event = _completed_checkout_event("cs_pass_flood")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(event).handle_checkout_session_completed(event)

        per_ticket_types = {
            NotificationType.TICKET_CREATED,
            NotificationType.TICKET_UPDATED,
            NotificationType.TICKET_CANCELLED,
            NotificationType.TICKET_REFUNDED,
        }
        offending_calls = [
            call
            for call in mock_notification_signal.call_args_list
            if call.kwargs["notification_type"] in per_ticket_types
        ]
        assert offending_calls == []

        purchased_calls = [
            call
            for call in mock_notification_signal.call_args_list
            if call.kwargs["notification_type"] == NotificationType.SERIES_PASS_PURCHASED
            and call.kwargs["user"] == revel_user
        ]
        assert len(purchased_calls) == 1

    @patch("notifications.signals.notification_requested.send")
    def test_regular_ticket_activation_still_sends_ticket_created(
        self,
        mock_notification_signal: Mock,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: object,
    ) -> None:
        """Regression: a REGULAR (non-pass) ticket must still notify staff on activation."""
        event = Event.objects.create(
            organization=stripe_connected_organization,
            name="Regular Flood Regression Event",
            slug="regular-flood-regression-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=timezone.now() + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Regular Flood Tier",
            price=Decimal("15.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name=revel_user.get_display_name(),
        )
        Payment.objects.create(
            ticket=ticket,
            user=revel_user,
            stripe_session_id="cs_regular_flood",
            amount=Decimal("15.00"),
            platform_fee=Decimal("1.00"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

        webhook_event = _completed_checkout_event("cs_regular_flood")
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            StripeEventHandler(webhook_event).handle_checkout_session_completed(webhook_event)

        created_calls = [
            call
            for call in mock_notification_signal.call_args_list
            if call.kwargs["notification_type"] == NotificationType.TICKET_CREATED
        ]
        assert len(created_calls) >= 1
