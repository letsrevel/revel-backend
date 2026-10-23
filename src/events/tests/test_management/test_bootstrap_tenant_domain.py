"""The e2e bootstrap seeds white-label tenant domains on dedicated orgs only (#1152).

The frontend 301s the platform URL of an org with an active custom domain, so the shared
fixture orgs (``eligibility-test-org``, Org Alpha) must never get one.
"""

import typing as t

import pytest
import structlog.testing
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.models import RevelUser
from events.management.commands.bootstrap_helpers import tenant_orgs
from events.management.commands.bootstrap_test_events import ORG_ALPHA_SLUG, Command
from events.models import Event, Organization, OrganizationDomain, TicketTier
from events.service import tenant_service
from geo.models import City

pytestmark = pytest.mark.django_db

PLATFORM_ACCOUNT = "acct_platform_test"


@pytest.fixture
def command(settings: t.Any) -> Command:
    """A command primed with the state ``handle`` would have, plus the shared fixture orgs."""
    settings.DEBUG = True  # *.localhost hostnames are only valid under DEBUG
    settings.STRIPE_ACCOUNT = PLATFORM_ACCOUNT
    owner = RevelUser.objects.create_user(username="owner@test.com", email="owner@test.com", password="x")
    cmd = Command()
    cmd.now = timezone.now()
    cmd.city = City.objects.create(
        name="Vienna", country="Austria", iso2="AT", iso3="AUT", city_id=1, location=Point(16.3738, 48.2082)
    )
    cmd.tenant_owner = RevelUser.objects.create_user(
        username="tenant.owner@example.com", email="tenant.owner@example.com", password="x"
    )
    cmd.org = Organization.objects.create(name="Org", slug="eligibility-test-org", owner=owner)
    Organization.objects.create(name="Alpha", slug=ORG_ALPHA_SLUG, owner=owner)
    return cmd


def test_dedicated_tenant_orgs_are_seeded_with_active_domains(command: Command) -> None:
    """Both tenant orgs are public, owned by the tenant persona, branded, and resolve by hostname."""
    command._create_tenant_domains()

    for slug, hostname, branding in [
        (tenant_orgs.TENANT_ORG_SLUG, "tenant.localhost", {"primary_color": "#B0306A", "accent_color": "#2E86DE"}),
        (tenant_orgs.TENANT_PAID_ORG_SLUG, "paid.localhost", {"primary_color": "#0E7C66", "accent_color": "#F2A541"}),
    ]:
        org = Organization.objects.get(slug=slug)
        assert org.owner == command.tenant_owner
        assert org.visibility == Organization.Visibility.PUBLIC
        assert org.custom_domain_enabled is True
        assert org.custom_domain == hostname
        assert org.domains.get().status == OrganizationDomain.Status.ACTIVE
        assert org.branding == branding
        resolved = tenant_service.get_tenant_organization(hostname)
        assert resolved is not None
        assert resolved.pk == org.pk


def test_free_tenant_org_has_an_rsvp_event_and_a_free_ticket_event(command: Command) -> None:
    """One RSVP-able event without tickets, one with a single free tier; both public, open and upcoming."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_ORG_SLUG)
    assert set(org.events.values_list("slug", flat=True)) == {"tenant-rsvp-event", "tenant-free-ticket-event"}
    for event in org.events.all():
        assert event.visibility == Event.Visibility.PUBLIC
        assert event.status == Event.EventStatus.OPEN
        assert event.start > command.now
    assert org.events.get(slug="tenant-rsvp-event").requires_ticket is False
    ticket_event = org.events.get(slug="tenant-free-ticket-event")
    assert ticket_event.requires_ticket is True
    tier = ticket_event.ticket_tiers.get()
    assert tier.name == "Free Ticket"
    assert tier.payment_method == TicketTier.PaymentMethod.FREE
    assert tier.price == 0


def test_paid_tenant_org_uses_the_platform_stripe_account(command: Command) -> None:
    """The paid org takes the platform account (unique, unused by Org Alpha) and sells one online tier."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_PAID_ORG_SLUG)
    assert org.stripe_account_id == PLATFORM_ACCOUNT
    assert org.stripe_charges_enabled is True
    assert org.stripe_details_submitted is True
    event = org.events.get()
    assert event.slug == "tenant-paid-event"
    assert event.status == Event.EventStatus.OPEN
    assert event.start > command.now
    tier = event.ticket_tiers.get()
    assert tier.name == "Standard Ticket"
    assert tier.payment_method == TicketTier.PaymentMethod.ONLINE
    assert tier.price == 10
    assert tier.currency == "EUR"


def test_shared_fixture_orgs_get_no_custom_domain(command: Command) -> None:
    """``eligibility-test-org`` and Org Alpha must stay on the platform host: no domain, gate closed."""
    command._create_tenant_domains()

    for slug in ["eligibility-test-org", ORG_ALPHA_SLUG]:
        org = Organization.objects.get(slug=slug)
        assert not org.domains.exists()
        assert org.custom_domain_enabled is False
        assert org.custom_domain is None
    assert OrganizationDomain.objects.count() == 2


def test_paid_org_is_still_created_with_a_warning_when_stripe_account_is_unset(
    command: Command, settings: t.Any
) -> None:
    """A placeholder ``STRIPE_ACCOUNT`` leaves the paid org without Stripe, and says so."""
    settings.STRIPE_ACCOUNT = "test_..."

    with structlog.testing.capture_logs() as logs:
        command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_PAID_ORG_SLUG)
    assert org.stripe_account_id is None
    assert org.domains.get().status == OrganizationDomain.Status.ACTIVE
    assert any(
        log["event"] == "tenant_paid_org_without_stripe" and log["reason"] == "stripe_account_unset" for log in logs
    )


def test_paid_org_is_still_created_with_a_warning_when_stripe_account_is_taken(command: Command) -> None:
    """If another org already holds the platform account (unique), the paid org goes without it."""
    Organization.objects.filter(slug=ORG_ALPHA_SLUG).update(stripe_account_id=PLATFORM_ACCOUNT)

    with structlog.testing.capture_logs() as logs:
        command._create_tenant_domains()

    assert Organization.objects.get(slug=tenant_orgs.TENANT_PAID_ORG_SLUG).stripe_account_id is None
    assert any(
        log["event"] == "tenant_paid_org_without_stripe" and log["reason"] == "stripe_account_in_use" for log in logs
    )


def test_tenant_domains_are_skipped_with_a_warning_without_debug(command: Command, settings: t.Any) -> None:
    """Without DEBUG ``*.localhost`` is not a valid hostname: log a skip instead of aborting the bootstrap."""
    settings.DEBUG = False

    with structlog.testing.capture_logs() as logs:
        command._create_tenant_domains()

    assert not OrganizationDomain.objects.exists()
    assert not Organization.objects.filter(slug__in=[tenant_orgs.TENANT_ORG_SLUG, tenant_orgs.TENANT_PAID_ORG_SLUG])
    assert {"event": "tenant_domains_skipped", "reason": "debug_disabled", "log_level": "warning"} in logs
