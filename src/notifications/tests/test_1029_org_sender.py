"""Tests for the org sender identity, List-Unsubscribe headers and suppression gating (#1029)."""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from django.core import mail
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from notifications.enums import (
    ORG_SENDER_TYPES,
    PLATFORM_LIST_UNSUBSCRIBE_TYPES,
    DeliveryChannel,
    DeliveryStatus,
    NotificationType,
)
from notifications.models import EmailSuppression, Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.email_policy import suppress
from notifications.service.org_sender import (
    build_list_unsubscribe_headers,
    org_from_address,
    org_reply_to,
    resolve_sender_org,
)

pytestmark = pytest.mark.django_db

DEFAULT_FROM = "Let's Revel <revel@letsrevel.io>"


@pytest.fixture(autouse=True)
def _email_settings(settings: t.Any) -> None:
    settings.DEFAULT_FROM_EMAIL = DEFAULT_FROM
    settings.ORG_EMAIL_DOMAIN = ""
    settings.BASE_URL = "https://api.example.test"


@pytest.fixture(autouse=True)
def _live_emails() -> None:
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()


def _decode(token: str) -> dict[str, t.Any]:
    return jwt.decode(token, options={"verify_signature": False})


def _make_notification(user: RevelUser, notification_type: str, context: dict[str, t.Any]) -> Notification:
    # context may not be blank; a filler key stands in for "no org/event ids".
    return Notification.objects.create(
        notification_type=notification_type, user=user, context=context or {"filler": True}
    )


def _make_delivery(notification: Notification) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )


def _mock_template() -> MagicMock:
    template = MagicMock()
    template.get_email_subject.return_value = "Subject"
    template.get_email_text_body.return_value = "Body"
    template.get_email_html_body.return_value = "<p>Body</p>"
    template.get_email_attachments.return_value = {}
    return template


# ---------------------------------------------------------------------------
# resolve_sender_org
# ---------------------------------------------------------------------------


class TestResolveSenderOrg:
    def test_from_organization_id(self, regular_user: RevelUser, organization: Organization) -> None:
        notif = _make_notification(
            regular_user, NotificationType.ORG_ANNOUNCEMENT, {"organization_id": str(organization.id)}
        )
        assert resolve_sender_org(notif) == organization

    def test_from_event_id(self, regular_user: RevelUser, public_event: Event) -> None:
        notif = _make_notification(regular_user, NotificationType.EVENT_OPEN, {"event_id": str(public_event.id)})
        assert resolve_sender_org(notif) == public_event.organization

    def test_none_when_context_has_no_ids(self, regular_user: RevelUser) -> None:
        notif = _make_notification(regular_user, NotificationType.EVENT_OPEN, {})
        assert resolve_sender_org(notif) is None

    def test_none_when_org_deleted(self, regular_user: RevelUser, organization: Organization) -> None:
        notif = _make_notification(
            regular_user, NotificationType.ORG_ANNOUNCEMENT, {"organization_id": str(organization.id)}
        )
        organization.delete()
        assert resolve_sender_org(notif) is None


# ---------------------------------------------------------------------------
# org_from_address / org_reply_to / headers
# ---------------------------------------------------------------------------


class TestOrgFromAddress:
    def test_falls_back_to_apex_domain_when_org_domain_unset(self, organization: Organization) -> None:
        assert org_from_address(organization) == "Test Org via Revel <test-org@letsrevel.io>"

    def test_uses_org_email_domain_when_set(self, organization: Organization, settings: t.Any) -> None:
        settings.ORG_EMAIL_DOMAIN = "mail.letsrevel.io"
        assert org_from_address(organization) == "Test Org via Revel <test-org@mail.letsrevel.io>"

    def test_strips_crlf_from_name(self, organization: Organization) -> None:
        organization.name = "Evil\r\nBcc: victim@example.com"
        address = org_from_address(organization)
        assert "\r" not in address and "\n" not in address
        assert address.endswith("<test-org@letsrevel.io>")

    @pytest.mark.parametrize("slug", ["abuse", "postmaster", "NoReply", "no-reply", "support", "revel", "info"])
    def test_role_name_slug_falls_back_to_default_sender(self, organization: Organization, slug: str) -> None:
        organization.slug = slug
        assert org_from_address(organization) == DEFAULT_FROM


class TestOrgReplyTo:
    def test_verified_contact_email(self, organization: Organization) -> None:
        organization.contact_email = "hello@org.example"
        organization.contact_email_verified = True
        assert org_reply_to(organization) == ["hello@org.example"]

    def test_unverified_contact_email(self, organization: Organization) -> None:
        organization.contact_email = "hello@org.example"
        organization.contact_email_verified = False
        assert org_reply_to(organization) == []

    def test_no_contact_email(self, organization: Organization) -> None:
        organization.contact_email = None
        organization.contact_email_verified = True
        assert org_reply_to(organization) == []


def test_build_list_unsubscribe_headers() -> None:
    headers = build_list_unsubscribe_headers("abc.def.ghi")
    assert headers == {
        "List-Unsubscribe": "<https://api.example.test/api/notification-preferences/one-click?token=abc.def.ghi>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


# ---------------------------------------------------------------------------
# EmailChannel.deliver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("notification_type", list(NotificationType))
@patch("notifications.service.templates.registry.get_template")
def test_deliver_sender_by_type(
    mock_get_template: MagicMock,
    notification_type: NotificationType,
    regular_user: RevelUser,
    organization: Organization,
) -> None:
    """Org sender types go out as the org with List-Unsubscribe; everything else as Revel."""
    mock_get_template.return_value = _mock_template()
    organization.contact_email = "hello@org.example"
    organization.contact_email_verified = True
    organization.save()
    notif = _make_notification(regular_user, notification_type, {"organization_id": str(organization.id)})
    delivery = _make_delivery(notif)

    assert EmailChannel().deliver(notif, delivery) is True

    msg = mail.outbox[-1]
    headers = msg.extra_headers
    if notification_type in ORG_SENDER_TYPES:
        assert msg.from_email == "Test Org via Revel <test-org@letsrevel.io>"
        assert msg.reply_to == ["hello@org.example"]
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert headers["List-Unsubscribe"].startswith(
            "<https://api.example.test/api/notification-preferences/one-click?token="
        )
        token = headers["List-Unsubscribe"].split("token=", 1)[1].rstrip(">")
        payload = _decode(token)
        assert payload["type"] == "unsubscribe"
        assert payload["notification_type"] == notification_type
        assert payload["organization_id"] == str(organization.id)
        assert headers["Feedback-ID"] == f"test-org:{notification_type}:revel"
        assert headers["X-Mailin-custom"] == f"delivery:{delivery.id}|org:{organization.id}"
    elif notification_type in PLATFORM_LIST_UNSUBSCRIBE_TYPES:
        assert msg.from_email == DEFAULT_FROM
        assert msg.reply_to == []  # ORG_NUDGE_REPLY_TO unset
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        payload = _decode(headers["List-Unsubscribe"].split("token=", 1)[1].rstrip(">"))
        assert payload["notification_type"] == notification_type
        assert payload["organization_id"] is None
        assert "Feedback-ID" not in headers
    else:
        assert msg.from_email == DEFAULT_FROM
        assert msg.reply_to == []
        assert "List-Unsubscribe" not in headers
        assert "Feedback-ID" not in headers
        assert headers["X-Mailin-custom"] == f"delivery:{delivery.id}"


@patch("notifications.service.templates.registry.get_template")
def test_deliver_org_sender_type_without_resolvable_org_uses_system_sender(
    mock_get_template: MagicMock, regular_user: RevelUser
) -> None:
    mock_get_template.return_value = _mock_template()
    notif = _make_notification(regular_user, NotificationType.EVENT_OPEN, {})
    delivery = _make_delivery(notif)

    assert EmailChannel().deliver(notif, delivery) is True

    msg = mail.outbox[-1]
    assert msg.from_email == DEFAULT_FROM
    assert "List-Unsubscribe" not in msg.extra_headers


@patch("notifications.service.templates.registry.get_template")
def test_deliver_resolves_org_through_event(
    mock_get_template: MagicMock, regular_user: RevelUser, public_event: Event
) -> None:
    mock_get_template.return_value = _mock_template()
    notif = _make_notification(regular_user, NotificationType.EVENT_REMINDER, {"event_id": str(public_event.id)})
    delivery = _make_delivery(notif)

    assert EmailChannel().deliver(notif, delivery) is True
    assert mail.outbox[-1].from_email == "Test Org via Revel <test-org@letsrevel.io>"
    assert mail.outbox[-1].reply_to == []


@pytest.mark.parametrize(
    "reason",
    [EmailSuppression.Reason.HARD_BOUNCE, EmailSuppression.Reason.COMPLAINT, EmailSuppression.Reason.BLOCKED],
)
@patch("notifications.service.templates.registry.get_template")
def test_deliver_to_suppressed_address_fails_without_sending(
    mock_get_template: MagicMock, reason: EmailSuppression.Reason, regular_user: RevelUser
) -> None:
    mock_get_template.return_value = _mock_template()
    suppress(regular_user.email, reason, EmailSuppression.Source.PROVIDER)
    notif = _make_notification(regular_user, NotificationType.TICKET_CREATED, {})
    delivery = _make_delivery(notif)

    assert EmailChannel().deliver(notif, delivery) is False

    assert mail.outbox == []
    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.metadata["suppression_reason"] == reason
    assert delivery.error_message == f"suppressed:{reason}"


@patch("notifications.service.templates.registry.get_template")
def test_invitation_opt_out_does_not_block_user_mail(mock_get_template: MagicMock, regular_user: RevelUser) -> None:
    mock_get_template.return_value = _mock_template()
    suppress(regular_user.email, EmailSuppression.Reason.INVITATION_OPT_OUT, EmailSuppression.Source.RECIPIENT)
    notif = _make_notification(regular_user, NotificationType.TICKET_CREATED, {})

    assert EmailChannel().deliver(notif, _make_delivery(notif)) is True
    assert len(mail.outbox) == 1


def test_retry_failed_deliveries_skips_suppressed(regular_user: RevelUser) -> None:
    from notifications.tasks import retry_failed_deliveries

    notif = _make_notification(regular_user, NotificationType.TICKET_CREATED, {})
    suppressed = NotificationDelivery.objects.create(
        notification=notif,
        channel=DeliveryChannel.EMAIL,
        status=DeliveryStatus.FAILED,
        metadata={"suppression_reason": "hard_bounce"},
    )
    transient = NotificationDelivery.objects.create(
        notification=notif, channel=DeliveryChannel.IN_APP, status=DeliveryStatus.FAILED
    )

    with patch("notifications.tasks.deliver_to_channel.delay") as mock_delay:
        result = retry_failed_deliveries()

    assert result == {"retried_count": 1}
    mock_delay.assert_called_once_with(str(transient.id))
    suppressed.refresh_from_db()
    assert suppressed.status == DeliveryStatus.FAILED


# ---------------------------------------------------------------------------
# Footer token, send_email headers, digest headers
# ---------------------------------------------------------------------------


def test_footer_unsubscribe_link_carries_typed_token(regular_user: RevelUser, organization: Organization) -> None:
    from notifications.service.templates.registry import get_template

    notif = _make_notification(
        regular_user, NotificationType.ORG_ANNOUNCEMENT, {"organization_id": str(organization.id)}
    )
    context = get_template(notif.notification_type)._get_template_context(notif)

    link = context["context"]["unsubscribe_link"]
    payload = _decode(link.split("token=", 1)[1])
    assert payload["type"] == "unsubscribe"
    assert payload["notification_type"] == NotificationType.ORG_ANNOUNCEMENT
    assert payload["organization_id"] == str(organization.id)


def test_send_email_passes_headers() -> None:
    from common.tasks import send_email

    send_email(to="a@example.com", subject="s", body="b", headers={"X-Mailin-custom": "k:v"})

    assert mail.outbox[-1].extra_headers["X-Mailin-custom"] == "k:v"


def test_digest_email_has_typeless_list_unsubscribe(regular_user: RevelUser) -> None:
    from notifications.service.digest import NotificationDigest

    _make_notification(regular_user, NotificationType.TICKET_CREATED, {})
    NotificationDigest(regular_user, Notification.objects.filter(user=regular_user)).send_digest_email()

    headers = mail.outbox[-1].extra_headers
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    payload = _decode(headers["List-Unsubscribe"].split("token=", 1)[1].rstrip(">"))
    assert payload["type"] == "unsubscribe"
    assert payload.get("notification_type") is None
    assert payload["exp"] > (timezone.now() + timedelta(days=365)).timestamp()
