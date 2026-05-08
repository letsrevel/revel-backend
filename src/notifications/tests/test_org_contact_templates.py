"""Tests for ORG_CONTACT_MESSAGE_RECEIVED template rendering.

Regression test for the issue where the in-app notification rendered with only
the title because the channel template files (in_app/telegram/email) were
missing — the base class defaults look them up by notification_type.
"""

import pytest

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.organization_templates import OrgContactMessageReceivedTemplate

pytestmark = pytest.mark.django_db


@pytest.fixture
def contact_notification(regular_user: RevelUser) -> Notification:
    """A notification populated with the same context the signal handler sends."""
    return Notification.objects.create(
        user=regular_user,
        notification_type=NotificationType.ORG_CONTACT_MESSAGE_RECEIVED,
        context={
            "message_id": "00000000-0000-0000-0000-000000000001",
            "organization_id": "00000000-0000-0000-0000-000000000002",
            "organization_name": "Acme Org",
            "sender_email": "sender@example.com",
            "subject": "Question about events",
            "message_preview": "Hi, can you tell me more about your upcoming workshop?",
            "admin_url": "https://example.test/org/acme/admin/contact-messages/abc",
        },
    )


def test_in_app_body_renders_sender_org_subject_and_link(contact_notification: Notification) -> None:
    """The in-app body must include the sender, org, subject, message preview, and admin link."""
    body = OrgContactMessageReceivedTemplate().get_in_app_body(contact_notification)
    assert "sender@example.com" in body
    assert "Acme Org" in body
    assert "Question about events" in body
    assert "Hi, can you tell me more" in body
    assert "https://example.test/org/acme/admin/contact-messages/abc" in body


def test_in_app_body_omits_subject_block_when_subject_is_empty(regular_user: RevelUser) -> None:
    """Subject is optional — body still renders cleanly without it."""
    notification = Notification.objects.create(
        user=regular_user,
        notification_type=NotificationType.ORG_CONTACT_MESSAGE_RECEIVED,
        context={
            "message_id": "id",
            "organization_id": "id",
            "organization_name": "Acme Org",
            "sender_email": "sender@example.com",
            "subject": "",
            "message_preview": "body only",
            "admin_url": "https://example.test/x",
        },
    )
    body = OrgContactMessageReceivedTemplate().get_in_app_body(notification)
    assert "Subject:" not in body
    assert "body only" in body


def test_telegram_body_renders_sender_org_and_admin_link(contact_notification: Notification) -> None:
    """Telegram body must surface enough context to act on the message."""
    body = OrgContactMessageReceivedTemplate().get_telegram_body(contact_notification)
    assert "sender@example.com" in body
    assert "Acme Org" in body
    assert "https://example.test/org/acme/admin/contact-messages/abc" in body


def test_email_text_body_renders_sender_org_and_admin_link(contact_notification: Notification) -> None:
    """Email text body must include the same details (used when a user opts EMAIL in)."""
    body = OrgContactMessageReceivedTemplate().get_email_text_body(contact_notification)
    assert "sender@example.com" in body
    assert "Acme Org" in body
    assert "https://example.test/org/acme/admin/contact-messages/abc" in body


def test_email_html_body_renders_sender_org_and_admin_link(contact_notification: Notification) -> None:
    """Email HTML body must include the same details."""
    body = OrgContactMessageReceivedTemplate().get_email_html_body(contact_notification)
    assert body is not None
    assert "sender@example.com" in body
    assert "Acme Org" in body
    assert "https://example.test/org/acme/admin/contact-messages/abc" in body
