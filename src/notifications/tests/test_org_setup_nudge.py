"""Rendering, envelope and command tests for ORG_SETUP_NUDGE (org setup nudges)."""

import datetime
import typing as t
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.utils import timezone, translation

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization, OrganizationNudge
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db

Trigger = OrganizationNudge.Trigger
LANGUAGES = ["en", "de", "it", "fr", "es", "pt"]


def _nudge(user: RevelUser, trigger: str, **extra: t.Any) -> Notification:
    context: dict[str, t.Any] = {
        "trigger": trigger,
        "is_last": False,
        "organization_name": "Night Owls",
        "organization_slug": "night-owls",
        "action_url": "https://app.example.test/org/night-owls/admin/settings",
        "can_reply": True,
        "event_name": "Summer party",
        "signature": "Sam",
        **extra,
    }
    return Notification.objects.create(notification_type=NotificationType.ORG_SETUP_NUDGE, user=user, context=context)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("trigger", [t.value for t in Trigger])
def test_every_trigger_renders_in_every_language(regular_user: RevelUser, trigger: str, language: str) -> None:
    regular_user.language = language
    regular_user.save(update_fields=["language"])
    notification = _nudge(regular_user, trigger)
    template = get_template(NotificationType.ORG_SETUP_NUDGE)

    with translation.override(language):
        subject = template.get_email_subject(notification)
        text = template.get_email_text_body(notification)
        in_app = template.get_in_app_body(notification)

    assert "Night Owls" in subject or "Summer party" in subject
    assert "%(" not in subject
    assert text.strip() and in_app.strip()
    assert "—" not in subject + text + in_app  # humanized copy: no em dashes
    if trigger != Trigger.CHECK_IN:
        assert notification.context["action_url"] in text


def test_copy_is_translated(regular_user: RevelUser) -> None:
    template = get_template(NotificationType.ORG_SETUP_NUDGE)
    notification = _nudge(regular_user, Trigger.PRIVATE_PROFILE)
    with translation.override("it"):
        assert template.get_email_subject(notification) == "Su Revel Night Owls è visibile solo a te"


def test_check_in_is_plain_text_and_signed(regular_user: RevelUser) -> None:
    template = get_template(NotificationType.ORG_SETUP_NUDGE)
    notification = _nudge(regular_user, Trigger.CHECK_IN, action_url="")

    assert template.get_email_html_body(notification) is None
    assert "Sam" in template.get_email_text_body(notification)


@pytest.mark.parametrize("trigger", [Trigger.DRAFT_EVENT, Trigger.PRIVATE_PROFILE, Trigger.NO_EVENTS])
def test_last_nudge_says_so(regular_user: RevelUser, trigger: str) -> None:
    template = get_template(NotificationType.ORG_SETUP_NUDGE)
    first = template.get_email_text_body(_nudge(regular_user, trigger, is_last=False))
    last = template.get_email_text_body(_nudge(regular_user, trigger, is_last=True))
    assert "last reminder" not in first
    assert "last reminder" in last


def test_reply_line_only_when_reply_to_is_configured(regular_user: RevelUser) -> None:
    template = get_template(NotificationType.ORG_SETUP_NUDGE)
    assert "reply to this email" in template.get_email_text_body(_nudge(regular_user, Trigger.NO_EVENTS))
    assert "reply to this email" not in template.get_email_text_body(
        _nudge(regular_user, Trigger.NO_EVENTS, can_reply=False)
    )


@patch("notifications.service.templates.registry.get_template")
def test_envelope_carries_reply_to_when_configured(
    mock_get_template: MagicMock, regular_user: RevelUser, settings: t.Any
) -> None:
    settings.ORG_NUDGE_REPLY_TO = "founder@example.com"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()
    mock_get_template.return_value = MagicMock(
        get_email_subject=MagicMock(return_value="s"),
        get_email_text_body=MagicMock(return_value="b"),
        get_email_html_body=MagicMock(return_value=None),
        get_email_attachments=MagicMock(return_value={}),
    )
    notification = _nudge(regular_user, Trigger.NO_EVENTS)
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )

    assert EmailChannel().deliver(notification, delivery) is True

    msg = mail.outbox[-1]
    assert msg.reply_to == ["founder@example.com"]
    assert "List-Unsubscribe" in msg.extra_headers


# --- Management command ---


@pytest.fixture
def stalled_org(regular_user: RevelUser) -> Organization:
    regular_user.email_verified = True
    regular_user.save(update_fields=["email_verified"])
    org = Organization.objects.create(name="Stalled", slug="stalled", owner=regular_user)
    Organization.objects.filter(pk=org.pk).update(created_at=timezone.now() - datetime.timedelta(days=21))
    return org


def test_command_dry_run_writes_nothing(stalled_org: Organization) -> None:
    out = StringIO()
    call_command("org_nudges", stdout=out)
    assert "stalled" in out.getvalue()
    assert "private_profile #1" in out.getvalue()
    assert not OrganizationNudge.objects.exists()
    assert not Notification.objects.exists()


@patch("notifications.tasks.dispatch_notification.delay")
def test_command_send_writes_the_log(mock_delay: MagicMock, stalled_org: Organization) -> None:
    call_command("org_nudges", "--send", "--org", "stalled", stdout=StringIO())
    assert OrganizationNudge.objects.filter(organization=stalled_org, trigger=Trigger.PRIVATE_PROFILE).exists()


def test_command_unknown_org() -> None:
    with pytest.raises(CommandError):
        call_command("org_nudges", "--org", "nope", stdout=StringIO())


def test_beat_task_ships_disabled() -> None:
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.get(name="Send org setup nudges")
    assert task.task == "events.send_org_nudges"
    assert task.enabled is False
