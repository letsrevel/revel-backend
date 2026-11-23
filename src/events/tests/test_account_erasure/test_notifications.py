"""An erased account gets only cancellation and refund emails, at the kept holder email (#1476)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail

from accounts.models import RevelUser
from accounts.tasks import delete_user_account
from common.email_backend import is_erased_address
from common.models import SiteSettings
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.tests.test_account_erasure.conftest import HOLDER_EMAIL, make_ticket
from notifications.enums import ERASED_ACCOUNT_EMAIL_TYPES, DeliveryChannel, NotificationType
from notifications.models import Notification
from notifications.service.channels.email import EmailChannel
from notifications.service.dispatcher import determine_delivery_channels
from notifications.service.email_policy import recipient_address, recipient_name

pytestmark = pytest.mark.django_db

Factory = t.Callable[[timedelta], Event]


@pytest.fixture
def erased_ticket(holder: RevelUser, ended_event_factory: Factory) -> Ticket:
    ticket = make_ticket(holder, ended_event_factory(-timedelta(days=5)))
    delete_user_account(str(holder.pk))
    return Ticket.objects.select_related("user", "event__organization", "tier").get(pk=ticket.pk)


def test_only_the_cancellation_and_refund_types_are_allowed() -> None:
    assert ERASED_ACCOUNT_EMAIL_TYPES == {
        NotificationType.TICKET_CANCELLED,
        NotificationType.TICKET_REFUNDED,
        NotificationType.TICKET_REFUND_FAILED,
        NotificationType.SERIES_PASS_CANCELLED,
        NotificationType.EVENT_CANCELLED,
    }


@pytest.mark.parametrize("notification_type", sorted(ERASED_ACCOUNT_EMAIL_TYPES))
def test_a_cancellation_type_goes_by_email_only(erased_ticket: Ticket, notification_type: str) -> None:
    assert determine_delivery_channels(erased_ticket.user, notification_type) == [DeliveryChannel.EMAIL]


@pytest.mark.parametrize(
    "notification_type",
    [NotificationType.EVENT_REMINDER, NotificationType.EVENT_UPDATED, NotificationType.SYSTEM_ANNOUNCEMENT],
)
def test_every_other_type_is_dropped(erased_ticket: Ticket, notification_type: str) -> None:
    """Reminders, updates and announcements reach no channel: no in-app row, no Telegram, no email."""
    assert determine_delivery_channels(erased_ticket.user, notification_type) == []
    notification = Notification(user=erased_ticket.user, notification_type=notification_type, context={})
    assert not EmailChannel().can_deliver(notification)


def test_the_recipient_is_the_kept_holder_email(erased_ticket: Ticket) -> None:
    assert recipient_address(erased_ticket.user) == HOLDER_EMAIL


def test_a_cancelled_ticket_mails_the_kept_email_and_never_the_placeholder(
    erased_ticket: Ticket, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The real cancel path: the status change sends TICKET_CANCELLED through the dispatcher."""
    site = SiteSettings.get_solo()
    site.live_emails = True  # addresses as typed, not rewritten to the internal catch-all
    site.save()
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        erased_ticket.status = Ticket.TicketStatus.CANCELLED
        erased_ticket.save(update_fields=["status"])

    recipients = [address for message in mail.outbox for address in message.recipients()]
    assert HOLDER_EMAIL in recipients
    assert not any(is_erased_address(address) for address in recipients)
    assert not Notification.objects.filter(
        user=erased_ticket.user, notification_type=NotificationType.TICKET_CANCELLED, deliveries__channel="in_app"
    ).exists()


def test_a_pass_only_holder_is_mailed_at_the_pass_snapshot(holder: RevelUser, event: Event) -> None:
    """An erased account that kept only a series pass still has a recipient for SERIES_PASS_CANCELLED."""
    assert event.event_series is not None
    series_pass = SeriesPass.objects.create(
        event_series=event.event_series,
        name="Season",
        price=Decimal("0"),
        pro_rata_discount=Decimal("0"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    HeldSeriesPass.objects.create(
        series_pass=series_pass, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE, price_paid=0
    )
    delete_user_account(str(holder.pk))
    erased = RevelUser.objects.get(pk=holder.pk)
    assert erased.erased_at is not None
    assert not Ticket.objects.filter(user=erased).exists()

    assert recipient_address(erased) == HOLDER_EMAIL
    assert recipient_name(erased) == "Ada L"
    assert determine_delivery_channels(erased, NotificationType.SERIES_PASS_CANCELLED) == [DeliveryChannel.EMAIL]


def test_a_ticket_only_holder_gets_a_neutral_greeting(
    erased_ticket: Ticket, django_capture_on_commit_callbacks: t.Any
) -> None:
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        erased_ticket.status = Ticket.TicketStatus.CANCELLED
        erased_ticket.save(update_fields=["status"])

    holder_mail = next(message for message in mail.outbox if HOLDER_EMAIL in message.recipients())
    # A ticket's guest_name may name the guest it was bought for, so it is never used to greet the holder.
    assert holder_mail.body.startswith("Hello,")
    assert "Deleted user" not in holder_mail.body


def test_a_cancelled_event_mails_the_erased_holder_once_at_the_kept_email(
    erased_ticket: Ticket, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The real event-cancel path: the erased holder is in the audience and gets exactly one EVENT_CANCELLED."""
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()
    event = erased_ticket.event
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        event.status = Event.EventStatus.CANCELLED
        event.save()

    to_holder = [message for message in mail.outbox if HOLDER_EMAIL in message.recipients()]
    assert len(to_holder) == 1
    assert Notification.objects.filter(user=erased_ticket.user).count() == 1
    assert Notification.objects.get(user=erased_ticket.user).notification_type == NotificationType.EVENT_CANCELLED
    assert not any(is_erased_address(a) for message in mail.outbox for a in message.recipients())


@pytest.mark.parametrize("bulk", [False, True])
def test_no_notification_row_is_stored_for_a_type_the_account_never_receives(erased_ticket: Ticket, bulk: bool) -> None:
    from notifications.service.dispatcher import NotificationData, bulk_create_notifications
    from notifications.signals import notification_requested

    context = {"k": "v"}
    if bulk:
        created = bulk_create_notifications(
            [NotificationData(NotificationType.EVENT_REMINDER, erased_ticket.user, context)]
        )
        assert created == []
    else:
        notification_requested.send(
            sender=test_no_notification_row_is_stored_for_a_type_the_account_never_receives,
            user=erased_ticket.user,
            notification_type=NotificationType.EVENT_REMINDER,
            context=context,
        )

    assert not Notification.objects.filter(user=erased_ticket.user).exists()
