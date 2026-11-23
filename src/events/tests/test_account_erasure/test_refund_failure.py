"""A refund of an erased holder that fails later: the holder and the staff are told at the kept email (#1476)."""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
import stripe
from django.core import mail

from accounts.models import RevelUser
from accounts.tasks import delete_user_account
from common.email_backend import is_erased_address
from common.models import SiteSettings
from events.models import Event, Payment, Ticket
from events.service.stripe_webhooks import handle_event
from events.tests.test_account_erasure.conftest import HOLDER_EMAIL, make_payment, make_ticket
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db

Factory = t.Callable[[timedelta], Event]


def _refund(payment: Payment, status: str) -> dict[str, t.Any]:
    return {
        "object": "refund",
        "id": "re_erased",
        "amount": 1000,
        "payment_intent": payment.stripe_payment_intent_id,
        "charge": "ch_erased",
        "metadata": {},
        "failure_reason": "expired_or_canceled_card" if status == "failed" else None,
        "status": status,
    }


def _deliver(event_type: str, obj: dict[str, t.Any], event_id: str, capture: t.Any) -> None:
    """Run the webhook entry point and its on-commit callbacks (the notification dispatch)."""
    payload = {"id": event_id, "type": event_type, "livemode": False, "data": {"object": obj}}
    with capture(execute=True):
        handle_event(stripe.Event.construct_from(payload, "sk_test"))


@pytest.fixture
def refunded_payment(
    holder: RevelUser, ended_event_factory: Factory, django_capture_on_commit_callbacks: t.Any
) -> t.Iterator[Payment]:
    """The kept payment of an erased holder, fully refunded at Stripe; the "refunded" email went out."""
    from events.tasks import generate_attendee_credit_note_task

    site = SiteSettings.get_solo()
    site.live_emails = True  # addresses as typed, not rewritten to the internal catch-all
    site.save()
    ticket = make_ticket(holder, ended_event_factory(-timedelta(days=5)))
    payment = make_payment(ticket)
    delete_user_account(str(holder.pk))
    assert RevelUser.objects.get(pk=holder.pk).erased_at is not None
    with patch.object(generate_attendee_credit_note_task, "delay"):
        charge = {
            "id": "ch_erased",
            "payment_intent": payment.stripe_payment_intent_id,
            "refunds": {"data": [_refund(payment, "succeeded")]},
        }
        _deliver("charge.refunded", charge, "evt_refunded", django_capture_on_commit_callbacks)
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED
        # The control: the erased holder was told that the ticket was refunded.
        assert Notification.objects.filter(user=holder, notification_type=NotificationType.TICKET_REFUNDED).exists()
        mail.outbox.clear()
        yield payment


def test_the_erased_holder_is_told_at_the_kept_email_that_the_refund_failed(
    refunded_payment: Payment, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    _deliver("refund.failed", _refund(refunded_payment, "failed"), "evt_failed", django_capture_on_commit_callbacks)

    refunded_payment.refresh_from_db()
    assert refunded_payment.status == Payment.PaymentStatus.SUCCEEDED  # the failure reopened the payment
    notification = Notification.objects.get(user=holder, notification_type=NotificationType.TICKET_REFUND_FAILED)
    assert not notification.deliveries.exclude(channel="email").exists()  # email only: no in-app row, no Telegram
    holder_mails = [message for message in mail.outbox if HOLDER_EMAIL in message.recipients()]
    assert len(holder_mails) == 1
    assert "failed" in holder_mails[0].subject
    assert holder_mails[0].body.startswith("Hello,")  # no name: a ticket's guest name is not the holder's
    assert "Deleted user" not in holder_mails[0].body
    assert not any(is_erased_address(address) for message in mail.outbox for address in message.recipients())


def test_the_staff_alert_names_the_kept_holder_email_and_not_the_placeholder(
    refunded_payment: Payment,
    holder: RevelUser,
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    _deliver("refund.failed", _refund(refunded_payment, "failed"), "evt_failed", django_capture_on_commit_callbacks)

    context = Notification.objects.get(
        user=organization_owner_user, notification_type=NotificationType.REFUND_FAILED
    ).context
    assert context["holder_email"] == HOLDER_EMAIL
    assert not is_erased_address(context["holder_email"])
    assert (
        Ticket.objects.get(pk=refunded_payment.ticket_id).user.email != HOLDER_EMAIL
    )  # the account has the placeholder


def test_the_staff_alert_of_a_live_account_still_has_the_account_email(
    holder: RevelUser,
    ended_event_factory: Factory,
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A live account has no kept email on the ticket: the alert falls back to the account email."""
    from events.tasks import generate_attendee_credit_note_task

    ticket = make_ticket(holder, ended_event_factory(-timedelta(days=5)))
    assert ticket.holder_email == ""
    payment = make_payment(ticket)
    with patch.object(generate_attendee_credit_note_task, "delay"):
        charge = {
            "id": "ch_erased",
            "payment_intent": payment.stripe_payment_intent_id,
            "refunds": {"data": [_refund(payment, "succeeded")]},
        }
        _deliver("charge.refunded", charge, "evt_refunded", django_capture_on_commit_callbacks)
        _deliver("refund.failed", _refund(payment, "failed"), "evt_failed", django_capture_on_commit_callbacks)

    context = Notification.objects.get(
        user=organization_owner_user, notification_type=NotificationType.REFUND_FAILED
    ).context
    assert context["holder_email"] == HOLDER_EMAIL
