"""Tests for the EVENT_REFUND_SUMMARY staff notification (Task 9).

Unlike REFUND_UNMATCHED (raised synchronously from the ``charge.refunded`` webhook),
this notification is a plain function called by ``events.send_event_refund_summary``
(the polling Celery task in ``events/tasks/refunds.py``) once a bulk
cancel-and-refund sweep finishes — there is no webhook fixture to drive it through,
so these tests exercise the function directly, mirroring
``test_stripe_webhook_refund_unmatched_notification.py``'s assertions on the
notification record and its rendered bodies.
"""

import typing as t

import pytest

from events.models import Event
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template
from notifications.signals.payment import send_event_refund_summary

pytestmark = pytest.mark.django_db


def _summary_notifications() -> list[Notification]:
    return list(Notification.objects.filter(notification_type=NotificationType.EVENT_REFUND_SUMMARY))


class TestSendEventRefundSummary:
    def test_notifies_the_organization_owner(self, event: Event, organization_owner_user: t.Any) -> None:
        send_event_refund_summary(event=event, cancelled=3, refunded=2, failed=1, still_active=0)

        notifications = _summary_notifications()
        assert len(notifications) == 1, "the organization owner must be told"
        assert notifications[0].user_id == organization_owner_user.id
        context = notifications[0].context
        assert context["organization_id"] == str(event.organization_id)
        assert context["organization_name"] == event.organization.name
        assert context["event_id"] == str(event.id)
        assert context["event_name"] == event.name
        assert context["cancelled"] == 3
        assert context["refunded"] == 2
        assert context["failed"] == 1
        assert context["still_active"] == 0

    def test_rendered_message_names_what_the_organizer_needs(
        self, event: Event, organization_owner_user: t.Any
    ) -> None:
        """Title and body must carry the event name and the sweep counts."""
        send_event_refund_summary(event=event, cancelled=3, refunded=1, failed=0, still_active=0)

        notification = _summary_notifications()[0]
        template = get_template(NotificationType.EVENT_REFUND_SUMMARY)
        title = template.get_in_app_title(notification)
        body = template.get_in_app_body(notification)

        assert event.name in title
        assert event.name in body
        assert "3" in body
        assert "1" in body
        # The other channels must render too — a missing template file is a silent
        # delivery failure for whichever channel the operator actually uses.
        assert template.get_email_subject(notification)
        assert template.get_email_text_body(notification)
        assert template.get_email_html_body(notification)
        assert template.get_telegram_body(notification)

    def test_still_active_surfaces_when_the_sweep_gave_up_waiting(
        self, event: Event, organization_owner_user: t.Any
    ) -> None:
        send_event_refund_summary(event=event, cancelled=1, refunded=1, failed=0, still_active=2)

        notification = _summary_notifications()[0]
        template = get_template(NotificationType.EVENT_REFUND_SUMMARY)
        body = template.get_in_app_body(notification)
        assert "2" in body
