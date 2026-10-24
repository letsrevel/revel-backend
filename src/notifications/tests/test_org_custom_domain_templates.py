"""Rendering, envelope and locales of the custom-domain owner mail (white-label, #1148)."""

import typing as t

import pytest
from django.conf import settings
from django.core import mail
from django.utils import translation

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db

ACTIVE = NotificationType.ORG_CUSTOM_DOMAIN_ACTIVE
FAILED = NotificationType.ORG_CUSTOM_DOMAIN_FAILED
LANGUAGES = ["en", "de", "it", "fr", "es", "pt"]
HOSTNAME = "events.example.org"
ADMIN_URL = "https://app.example.test/org/night-owls/admin/custom-domain"
ERRORS = ["No CNAME record was found for events.example.org; add one pointing to customers.letsrevel.io."]


def _notification(user: RevelUser, notification_type: NotificationType, **extra: t.Any) -> Notification:
    base: dict[str, t.Any] = {
        "organization_id": "00000000-0000-0000-0000-000000000000",
        "organization_name": "Night Owls",
        "organization_slug": "night-owls",
        "hostname": HOSTNAME,
        "action_url": ADMIN_URL,
    }
    if notification_type == ACTIVE:
        base["domain_url"] = f"https://{HOSTNAME}"
    else:
        base.update(was_live=False, errors=ERRORS)
    return Notification.objects.create(notification_type=notification_type, user=user, context={**base, **extra})


@pytest.mark.parametrize("language", LANGUAGES)
def test_active_renders_in_every_language(regular_user: RevelUser, language: str) -> None:
    notification = _notification(regular_user, ACTIVE)
    template = get_template(ACTIVE)

    with translation.override(language):
        subject = template.get_email_subject(notification)
        text = template.get_email_text_body(notification)
        html = template.get_email_html_body(notification) or ""
        in_app = template.get_in_app_body(notification)
        telegram = template.get_telegram_body(notification)

    assert HOSTNAME in subject and "Night Owls" in subject
    assert "%(" not in subject
    for body in (text, html, in_app, telegram):
        assert f"https://{HOSTNAME}" in body
        assert ADMIN_URL in body
        assert "—" not in body  # humanized copy: no em dashes
    assert text.count("\n- ") == 3  # the checklist: test the site, sign up, share the link


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("was_live", [False, True])
def test_failed_renders_in_every_language(regular_user: RevelUser, language: str, was_live: bool) -> None:
    notification = _notification(regular_user, FAILED, was_live=was_live)
    template = get_template(FAILED)

    with translation.override(language):
        subject = template.get_email_subject(notification)
        text = template.get_email_text_body(notification)
        html = template.get_email_html_body(notification) or ""
        in_app = template.get_in_app_body(notification)
        telegram = template.get_telegram_body(notification)

    assert HOSTNAME in subject and "Night Owls" in subject
    assert "%(" not in subject
    for body in (text, html, in_app, telegram):
        assert ERRORS[0] in body
        assert ADMIN_URL in body
        assert "—" not in body
    assert f"https://{HOSTNAME}" not in text  # the failed mail never links the tenant domain


@pytest.mark.parametrize(
    ("language", "active_subject", "stopped_subject", "pending_subject"),
    [
        (
            "it",
            f"{HOSTNAME} è ora attivo per Night Owls",
            f"{HOSTNAME} ha smesso di funzionare per Night Owls",
            f"Non è stato possibile attivare {HOSTNAME} per Night Owls",
        ),
        (
            "de",
            f"{HOSTNAME} ist jetzt für Night Owls live",
            f"{HOSTNAME} funktioniert für Night Owls nicht mehr",
            f"{HOSTNAME} konnte für Night Owls nicht aktiviert werden",
        ),
    ],
)
def test_subjects_and_bodies_are_translated(
    regular_user: RevelUser, language: str, active_subject: str, stopped_subject: str, pending_subject: str
) -> None:
    active = _notification(regular_user, ACTIVE)
    stopped = _notification(regular_user, FAILED, was_live=True)
    pending = _notification(regular_user, FAILED, was_live=False)

    with translation.override(language):
        assert get_template(ACTIVE).get_email_subject(active) == active_subject
        assert get_template(FAILED).get_email_subject(stopped) == stopped_subject
        assert get_template(FAILED).get_email_subject(pending) == pending_subject
        # The body is translated too (not left as the English msgid).
        assert "Open your site" not in get_template(ACTIVE).get_email_text_body(active)
        assert "What Revel found" not in get_template(FAILED).get_email_text_body(pending)


def test_failed_subject_and_copy_depend_on_whether_the_domain_was_live(regular_user: RevelUser) -> None:
    template = get_template(FAILED)
    pending = _notification(regular_user, FAILED, was_live=False)
    live = _notification(regular_user, FAILED, was_live=True)

    assert template.get_email_subject(pending) == f"{HOSTNAME} could not be activated for Night Owls"
    assert template.get_email_subject(live) == f"{HOSTNAME} stopped working for Night Owls"
    assert "signed out" in template.get_email_text_body(live)
    assert "signed out" not in template.get_email_text_body(pending)


def test_failed_without_reasons_says_so_and_points_to_support(regular_user: RevelUser) -> None:
    notification = _notification(regular_user, FAILED, errors=[])

    text = get_template(FAILED).get_email_text_body(notification)

    assert "No details were reported" in text
    assert "contact Revel support" in text


def test_reasons_are_escaped_in_html(regular_user: RevelUser) -> None:
    notification = _notification(regular_user, FAILED, errors=["<script>alert(1)</script>"])

    html = get_template(FAILED).get_email_html_body(notification) or ""

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("notification_type", [ACTIVE, FAILED])
def test_email_goes_out_from_the_platform_with_one_click_unsubscribe(
    regular_user: RevelUser, notification_type: NotificationType
) -> None:
    """Revel's own mail to the owner: system sender, org branding in the body, platform unsubscribe link."""
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()
    org = Organization.objects.create(
        name="Night Owls", slug="night-owls", owner=regular_user, custom_domain=HOSTNAME, custom_domain_enabled=True
    )
    notification = _notification(
        regular_user, notification_type, organization_id=str(org.id), action_url=f"{site.frontend_base_url}/x"
    )
    delivery = NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.PENDING
    )

    assert EmailChannel().deliver(notification, delivery) is True

    message = mail.outbox[-1]
    assert message.from_email == settings.DEFAULT_FROM_EMAIL
    assert "via Revel" not in message.from_email
    assert "List-Unsubscribe-Post" in message.extra_headers
    assert "Night Owls" in message.body
    unsubscribe_links = [line for line in message.body.splitlines() if "unsubscribe" in line.lower()]
    assert unsubscribe_links
    assert all(HOSTNAME not in line for line in unsubscribe_links)
