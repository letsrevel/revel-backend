"""Message-ID uses the sending domain, not the container hostname (#1037)."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail

from accounts.models import RevelUser
from common.models import SiteSettings
from common.tasks import send_email
from common.utils import with_message_id
from events.models import Organization
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _email_settings(settings: t.Any) -> None:
    settings.DEFAULT_FROM_EMAIL = "Let's Revel <revel@letsrevel.io>"
    settings.ORG_EMAIL_DOMAIN = "mail.letsrevel.io"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()


def _msgid_domain(message: t.Any) -> str:
    """Domain part of the Message-ID actually rendered into the MIME message."""
    return str(message.message()["Message-ID"]).strip("<>").rpartition("@")[2]


def _deliver(user: RevelUser, notification_type: str, organization: Organization) -> t.Any:
    template = MagicMock()
    template.get_email_subject.return_value = "Subject"
    template.get_email_text_body.return_value = "Body"
    template.get_email_html_body.return_value = "<p>Body</p>"
    template.get_email_attachments.return_value = {}
    notif = Notification.objects.create(
        notification_type=notification_type, user=user, context={"organization_id": str(organization.id)}
    )
    delivery = NotificationDelivery.objects.create(
        notification=notif, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )
    with patch("notifications.service.templates.registry.get_template", return_value=template):
        assert EmailChannel().deliver(notif, delivery) is True
    return mail.outbox[-1]


def test_org_sender_message_id_on_org_domain(regular_user: RevelUser, organization: Organization) -> None:
    msg = _deliver(regular_user, NotificationType.ORG_ANNOUNCEMENT, organization)

    assert msg.from_email == "Test Org via Revel <test-org@mail.letsrevel.io>"
    assert _msgid_domain(msg) == "mail.letsrevel.io"


def test_system_sender_message_id_on_apex_domain(regular_user: RevelUser, organization: Organization) -> None:
    msg = _deliver(regular_user, NotificationType.TICKET_CREATED, organization)

    assert msg.from_email == "Let's Revel <revel@letsrevel.io>"
    assert _msgid_domain(msg) == "letsrevel.io"


def test_send_email_message_id_on_default_sender_domain() -> None:
    send_email(to="a@example.com", subject="s", body="b")

    assert _msgid_domain(mail.outbox[-1]) == "letsrevel.io"


def test_send_email_message_id_follows_explicit_from() -> None:
    send_email(to="a@example.com", subject="s", body="b", from_email="Org via Revel <org@mail.letsrevel.io>")

    assert _msgid_domain(mail.outbox[-1]) == "mail.letsrevel.io"


def test_send_email_keeps_caller_message_id() -> None:
    send_email(to="a@example.com", subject="s", body="b", headers={"message-id": "<fixed@example.org>"})

    msg = mail.outbox[-1]
    assert msg.extra_headers == {"message-id": "<fixed@example.org>"}
    assert msg.message()["Message-ID"] == "<fixed@example.org>"


def test_with_message_id_does_not_mutate_input() -> None:
    headers = {"X-Mailin-custom": "k:v"}

    result = with_message_id(headers, "a@b.example")

    assert headers == {"X-Mailin-custom": "k:v"}
    assert result["X-Mailin-custom"] == "k:v"
    assert result["Message-ID"].endswith("@b.example>")
