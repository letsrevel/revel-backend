"""Attendee-path links built outside Stripe and notification enrichment follow the org domain (#1153)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Announcement, Event, Organization
from events.schema import TicketPurchaseItem
from events.service.announcement_service import _build_notification_context
from events.service.guest import create_guest_rsvp_token, create_guest_ticket_token
from events.tasks import send_guest_rsvp_confirmation, send_guest_ticket_confirmation
from events.tasks.attendees import _confirmation_org

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


def _event(org: Organization) -> Event:
    return Event.objects.create(
        organization=org,
        name="E",
        slug="e",
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
    )


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

    def test_org_derived_from_token_when_not_given(self, organization: Organization, user: RevelUser) -> None:
        """Producers send the pre-epic arguments; the task resolves the tenant from the token's event."""
        _set_domain(organization)
        event = _event(organization)
        rsvp_token = create_guest_rsvp_token(user, event.id, "yes")
        ticket_token = create_guest_ticket_token(
            user, event.id, tier_id=uuid4(), tickets=[TicketPurchaseItem(guest_name="G")]
        )

        send_guest_rsvp_confirmation("guest@example.com", rsvp_token, "Party")
        send_guest_ticket_confirmation("guest@example.com", ticket_token, "Party", "General")

        assert f"https://{HOST}/events/confirm-action?token={rsvp_token}" in mail.outbox[0].body
        assert f"https://{HOST}/events/confirm-action?token={ticket_token}" in mail.outbox[1].body

    def test_org_derivation_costs_one_query(self, organization: Organization, user: RevelUser) -> None:
        token = create_guest_rsvp_token(user, _event(organization).id, "yes")

        with CaptureQueriesContext(connection) as ctx:
            _confirmation_org(None, token)

        assert len(ctx) == 1

    def test_explicit_organization_id_wins_over_token(self, organization: Organization) -> None:
        _set_domain(organization)

        send_guest_rsvp_confirmation("guest@example.com", "not-a-jwt", "Party", str(organization.id))

        assert f"https://{HOST}/events/confirm-action?token=not-a-jwt" in mail.outbox[0].body

    def test_unreadable_token_without_org_uses_platform(self) -> None:
        send_guest_rsvp_confirmation("guest@example.com", "not-a-jwt", "Party")

        assert f"{PLATFORM}/events/confirm-action?token=not-a-jwt" in mail.outbox[0].body

    def test_unknown_organization_id_uses_platform(self) -> None:
        send_guest_rsvp_confirmation("guest@example.com", "tok", "Party", "33333333-3333-3333-3333-333333333333")

        assert f"{PLATFORM}/events/confirm-action?token=tok" in mail.outbox[0].body


def test_announcement_context_uses_tenant_origin(organization: Organization, user: RevelUser) -> None:
    _set_domain(organization)
    announcement = Announcement.objects.create(organization=organization, title="Hi", body="Hello", created_by=user)

    ctx = _build_notification_context(announcement)

    assert ctx["organization_url"] == f"https://{HOST}/org/{organization.slug}"
