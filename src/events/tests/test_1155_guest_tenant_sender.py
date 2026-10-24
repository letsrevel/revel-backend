"""Guest confirmation emails use the org sender for white-label tenants only (#1155)."""

import typing as t

import pytest
from django.core import mail

from common.models import SiteSettings
from events.models import Organization
from events.tasks import send_guest_rsvp_confirmation, send_guest_ticket_confirmation

pytestmark = pytest.mark.django_db

HOST = "events.example.com"
DEFAULT_FROM = "Let's Revel <revel@letsrevel.io>"


@pytest.fixture(autouse=True)
def _email_settings(settings: t.Any) -> None:
    settings.DEFAULT_FROM_EMAIL = DEFAULT_FROM
    settings.ORG_EMAIL_DOMAIN = "mail.letsrevel.io"
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()


def kind_label(kind: str) -> str:
    return "rsvp_confirmation" if kind == "rsvp" else "ticket_confirmation"


def _send(kind: str, organization_id: str | None) -> None:
    if kind == "rsvp":
        send_guest_rsvp_confirmation("guest@example.com", "tok", "Party", organization_id)
    else:
        send_guest_ticket_confirmation("guest@example.com", "tok", "Party", "General", organization_id)


@pytest.mark.parametrize("kind", ["rsvp", "ticket"])
class TestGuestConfirmationSender:
    def test_tenant_org_sends_as_org_with_verified_reply_to(self, organization: Organization, kind: str) -> None:
        Organization.objects.filter(pk=organization.pk).update(
            custom_domain=HOST,
            custom_domain_enabled=True,
            contact_email="hello@org.example",
            contact_email_verified=True,
        )

        _send(kind, str(organization.id))

        msg = mail.outbox[0]
        assert msg.from_email == f"{organization.name} via Revel <{organization.slug}@mail.letsrevel.io>"
        assert msg.reply_to == ["hello@org.example"]
        assert msg.extra_headers["Feedback-ID"] == f"{organization.slug}:guest_{kind_label(kind)}:revel"
        assert msg.extra_headers["X-Mailin-custom"] == f"org:{organization.id}"
        assert msg.extra_headers["List-Unsubscribe"].startswith("<")
        assert HOST not in msg.extra_headers["List-Unsubscribe"]

    def test_non_tenant_org_keeps_platform_sender(self, organization: Organization, kind: str) -> None:
        Organization.objects.filter(pk=organization.pk).update(
            contact_email="hello@org.example", contact_email_verified=True
        )

        _send(kind, str(organization.id))

        assert mail.outbox[0].from_email == DEFAULT_FROM
        assert mail.outbox[0].reply_to == []

    def test_kill_switch_off_keeps_platform_sender(self, organization: Organization, kind: str) -> None:
        Organization.objects.filter(pk=organization.pk).update(custom_domain=HOST, custom_domain_enabled=False)

        _send(kind, str(organization.id))

        assert mail.outbox[0].from_email == DEFAULT_FROM

    def test_omitted_organization_keeps_platform_sender(self, kind: str) -> None:
        _send(kind, None)

        assert mail.outbox[0].from_email == DEFAULT_FROM
