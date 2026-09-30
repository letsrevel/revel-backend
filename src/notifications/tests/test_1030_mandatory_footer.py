"""Mandatory mail can't be opted out of, so its footer must not promise "unsubscribe" (#1030)."""

import pytest
from django.template.loader import render_to_string

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db


def _footer(notification: Notification) -> tuple[str, str]:
    context = get_template(notification.notification_type)._get_template_context(notification)
    return (
        render_to_string("notifications/email/_footer.txt", context),
        render_to_string("notifications/email/_footer.html", context),
    )


def test_mandatory_type_footer_offers_preferences_only(notification: Notification) -> None:
    notification.notification_type = NotificationType.TICKET_CREATED

    for body in _footer(notification):
        assert "Manage notification preferences" in body
        assert "Unsubscribe" not in body


def test_ordinary_type_footer_offers_unsubscribe(notification: Notification) -> None:
    notification.notification_type = NotificationType.EVENT_UPDATED

    for body in _footer(notification):
        assert "Unsubscribe or manage notification preferences" in body
