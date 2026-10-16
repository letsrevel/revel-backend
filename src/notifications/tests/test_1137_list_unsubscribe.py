"""Every email carries a Revel List-Unsubscribe header so Brevo never injects its own (#1137)."""

import typing as t
from unittest.mock import MagicMock, patch

import jwt
import pytest
from django.core import mail
from django.test import Client

from accounts.models import RevelUser
from common.models import SiteSettings
from notifications.enums import MANDATORY_TYPES, ORG_SENDER_TYPES, DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel

pytestmark = pytest.mark.django_db

DEFAULT_FROM = "Let's Revel <revel@letsrevel.io>"
ONE_CLICK = "https://api.example.test/api/notification-preferences/one-click?token="


@pytest.fixture(autouse=True)
def _email_settings(settings: t.Any) -> None:
    settings.DEFAULT_FROM_EMAIL = DEFAULT_FROM
    settings.ORG_EMAIL_DOMAIN = ""
    settings.BASE_URL = "https://api.example.test"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()


def _envelope(user: RevelUser, notification_type: str) -> tuple[str, list[str], dict[str, str]]:
    notification = Notification.objects.create(notification_type=notification_type, user=user, context={"filler": True})
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )
    return EmailChannel._envelope(notification, delivery)


def _token(header: str) -> str:
    return header.split("token=", 1)[1].rstrip(">")


@pytest.mark.parametrize("notification_type", list(NotificationType))
def test_every_type_gets_a_revel_list_unsubscribe(regular_user: RevelUser, notification_type: str) -> None:
    _from, _reply_to, headers = _envelope(regular_user, notification_type)

    if notification_type in MANDATORY_TYPES:
        frontend = SiteSettings.get_solo().frontend_base_url
        assert headers["List-Unsubscribe"].startswith(f"<{frontend}/unsubscribe?token=")
        assert "List-Unsubscribe-Post" not in headers
    else:
        assert headers["List-Unsubscribe"].startswith(f"<{ONE_CLICK}")
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    payload = jwt.decode(_token(headers["List-Unsubscribe"]), options={"verify_signature": False})
    assert payload["notification_type"] == notification_type


def test_org_sender_type_without_resolvable_org_gets_typed_one_click(regular_user: RevelUser) -> None:
    notification_type = next(iter(ORG_SENDER_TYPES - MANDATORY_TYPES))

    sender, reply_to, headers = _envelope(regular_user, notification_type)

    assert sender == DEFAULT_FROM
    assert reply_to == []
    assert headers["List-Unsubscribe"].startswith(f"<{ONE_CLICK}")
    assert "Feedback-ID" not in headers


@patch("notifications.service.templates.registry.get_template")
def test_delivered_mandatory_email_has_preferences_header_only(
    mock_get_template: MagicMock, regular_user: RevelUser
) -> None:
    template = MagicMock()
    template.get_email_subject.return_value = "Subject"
    template.get_email_text_body.return_value = "Body"
    template.get_email_html_body.return_value = "<p>Body</p>"
    template.get_email_attachments.return_value = {}
    mock_get_template.return_value = template
    notification = Notification.objects.create(
        notification_type=NotificationType.TICKET_CREATED, user=regular_user, context={"filler": True}
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )

    assert EmailChannel().deliver(notification, delivery) is True

    headers = mail.outbox[-1].extra_headers
    assert "/unsubscribe?token=" in headers["List-Unsubscribe"]
    assert "List-Unsubscribe-Post" not in headers


def test_one_click_of_staff_alert_header_disables_only_that_type(client: Client, regular_user: RevelUser) -> None:
    staff_type = NotificationType.STAFF_TICKET_CREATED
    assert staff_type not in MANDATORY_TYPES
    _from, _reply_to, headers = _envelope(regular_user, staff_type)

    response = client.post(
        f"/api/notification-preferences/one-click?token={_token(headers['List-Unsubscribe'])}",
        data="List-Unsubscribe=One-Click",
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 200, response.content
    prefs = regular_user.notification_preferences
    prefs.refresh_from_db()
    assert DeliveryChannel.EMAIL not in prefs.get_channels_for_notification_type(staff_type)
    other = NotificationType.STAFF_TICKET_CANCELLED
    assert DeliveryChannel.EMAIL in prefs.get_channels_for_notification_type(other)
