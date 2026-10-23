"""Attendee-path links built outside Stripe and notification enrichment follow the org domain (#1153)."""

import pytest
from django.core import mail

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Announcement, Organization
from events.service.announcement_service import _build_notification_context
from events.tasks import send_guest_rsvp_confirmation, send_guest_ticket_confirmation

pytestmark = pytest.mark.django_db

HOST = "events.example.com"
PLATFORM = "https://app.letsrevel.io"


@pytest.fixture(autouse=True)
def platform_url() -> None:
    site = SiteSettings.get_solo()
    site.frontend_base_url = PLATFORM
    site.save()


def _set_domain(org: Organization, *, enabled: bool = True) -> None:
    Organization.objects.filter(pk=org.pk).update(custom_domain=HOST, custom_domain_enabled=enabled)
    org.refresh_from_db()


class TestGuestConfirmationLinks:
    """The guest confirm-action link points at the event organization's tenant origin."""

    def test_rsvp_link_uses_tenant_origin(self, organization: Organization) -> None:
        _set_domain(organization)

        send_guest_rsvp_confirmation("guest@example.com", "tok", "Party", str(organization.id))

        assert f"https://{HOST}/events/confirm-action?token=tok" in mail.outbox[0].body

    def test_ticket_link_uses_tenant_origin(self, organization: Organization) -> None:
        _set_domain(organization)

        send_guest_ticket_confirmation("guest@example.com", "tok", "Party", "General", str(organization.id))

        assert f"https://{HOST}/events/confirm-action?token=tok" in mail.outbox[0].body

    def test_kill_switch_off_uses_platform(self, organization: Organization) -> None:
        _set_domain(organization, enabled=False)

        send_guest_rsvp_confirmation("guest@example.com", "tok", "Party", str(organization.id))

        assert f"{PLATFORM}/events/confirm-action?token=tok" in mail.outbox[0].body

    def test_omitted_organization_id_uses_platform(self) -> None:
        """Tasks queued before the signature grew an organization_id keep working."""
        send_guest_ticket_confirmation("guest@example.com", "tok", "Party", "General")

        assert f"{PLATFORM}/events/confirm-action?token=tok" in mail.outbox[0].body

    def test_unknown_organization_id_uses_platform(self) -> None:
        send_guest_rsvp_confirmation("guest@example.com", "tok", "Party", "33333333-3333-3333-3333-333333333333")

        assert f"{PLATFORM}/events/confirm-action?token=tok" in mail.outbox[0].body


def test_announcement_context_uses_tenant_origin(organization: Organization, user: RevelUser) -> None:
    _set_domain(organization)
    announcement = Announcement.objects.create(organization=organization, title="Hi", body="Hello", created_by=user)

    ctx = _build_notification_context(announcement)

    assert ctx["organization_url"] == f"https://{HOST}/org/{organization.slug}"
