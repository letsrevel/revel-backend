"""Tests for the org sender, opt-out link and suppression on pending-invitation mail (#1029)."""

import typing as t

import jwt
import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives

from common.models import SiteSettings
from events.models import Event, PendingEventInvitation
from notifications.models import EmailSuppression
from notifications.service.email_policy import suppress
from notifications.tasks import send_pending_invitation_email

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _email_settings(settings: t.Any) -> None:
    settings.DEFAULT_FROM_EMAIL = "Let's Revel <revel@letsrevel.io>"
    settings.ORG_EMAIL_DOMAIN = ""
    settings.BASE_URL = "https://api.example.test"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()


@pytest.fixture
def pending(public_event: Event) -> PendingEventInvitation:
    org = public_event.organization
    org.contact_email = "hello@org.example"
    org.contact_email_verified = True
    org.save()
    return PendingEventInvitation.objects.create(event=public_event, email="invitee@example.com")


def test_sends_as_org_with_opt_out_header_and_link(pending: PendingEventInvitation) -> None:
    org = pending.event.organization
    frontend = SiteSettings.get_solo().frontend_base_url

    send_pending_invitation_email.apply(args=(str(pending.id),))

    msg = mail.outbox[-1]
    assert msg.from_email == "Test Org via Revel <test-org@letsrevel.io>"
    assert msg.reply_to == ["hello@org.example"]
    headers = msg.extra_headers
    assert headers["X-Mailin-custom"] == f"invitation:{pending.id}|org:{org.id}"
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    token = headers["List-Unsubscribe"].split("token=", 1)[1].rstrip(">")
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["type"] == "email_opt_out"
    assert payload["email"] == "invitee@example.com"
    assert payload["organization_id"] == str(org.id)

    html = str(t.cast(EmailMultiAlternatives, msg).alternatives[0][0])
    opt_out_prefix = f"{frontend}/unsubscribe?token="
    assert opt_out_prefix in msg.body
    assert opt_out_prefix in html
    assert "Stop these emails" in msg.body
    assert "Stop these emails" in html


@pytest.mark.parametrize("reason", [EmailSuppression.Reason.INVITATION_OPT_OUT, EmailSuppression.Reason.HARD_BOUNCE])
def test_suppressed_address_is_not_sent(pending: PendingEventInvitation, reason: EmailSuppression.Reason) -> None:
    suppress("Invitee@Example.com", reason, EmailSuppression.Source.RECIPIENT)

    send_pending_invitation_email.apply(args=(str(pending.id),))

    assert mail.outbox == []
