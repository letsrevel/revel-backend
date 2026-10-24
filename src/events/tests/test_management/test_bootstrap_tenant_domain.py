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
from events.models import Event, Organization, OrganizationDomain, TicketTier, VenueSector
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
    """The tenant orgs are public, owned by the tenant persona, branded, and resolve by hostname."""
    command._create_tenant_domains()

    for slug, hostname, branding in [
        (tenant_orgs.TENANT_ORG_SLUG, "tenant.localhost", {"primary_color": "#B0306A", "accent_color": "#2E86DE"}),
        (tenant_orgs.TENANT_PAID_ORG_SLUG, "paid.localhost", {"primary_color": "#0E7C66", "accent_color": "#F2A541"}),
        (
            tenant_orgs.TENANT_REVOKE_ORG_SLUG,
            "revoke.localhost",
            {"primary_color": "#5B3FB0", "accent_color": "#E0A21B"},
        ),
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
    """One RSVP-able event without tickets, one with a single free tier, one seated; all public, open, upcoming."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_ORG_SLUG)
    assert set(org.events.values_list("slug", flat=True)) == {
        "tenant-rsvp-event",
        "tenant-free-ticket-event",
        "tenant-seated-event",
    }
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
    event = org.events.get(slug="tenant-paid-event")
    assert event.status == Event.EventStatus.OPEN
    assert event.start > command.now
    tier = event.ticket_tiers.get()
    assert tier.name == "Standard Ticket"
    assert tier.payment_method == TicketTier.PaymentMethod.ONLINE
    assert tier.price == 10
    assert tier.currency == "EUR"


def _assert_seated_event(event: Event, org: Organization, tier_name: str, payment_method: str, price: int) -> None:
    """Public, open, upcoming and guest-purchasable, with one USER_CHOICE tier over a 3 x 6 seat map (A1..C6)."""
    assert event.organization == org
    assert event.visibility == Event.Visibility.PUBLIC
    assert event.status == Event.EventStatus.OPEN
    assert event.start > timezone.now()
    assert event.requires_ticket is True
    assert event.can_attend_without_login is True
    venue = event.venue
    assert venue is not None
    assert venue.organization == org

    tier = event.ticket_tiers.get()
    assert tier.name == tier_name
    assert tier.payment_method == payment_method
    assert tier.price == price
    assert tier.currency == "EUR"
    assert tier.seat_assignment_mode == TicketTier.SeatAssignmentMode.USER_CHOICE
    assert tier.venue == venue
    assert tier.total_quantity == 18
    assert tier.visibility == TicketTier.Visibility.PUBLIC
    assert tier.purchasable_by == TicketTier.PurchasableBy.PUBLIC

    sector = venue.sectors.get()
    assert tier.sector == sector
    assert sector.kind == VenueSector.Kind.SEATED
    assert (sector.name, sector.code) == ("Main Floor", "MAIN")
    seats = sector.seats.order_by("row_order", "number")
    assert seats.count() == 18
    assert all(seat.is_active for seat in seats)
    assert [(seat.row_label, seat.number, seat.label) for seat in seats] == [
        (row, n, f"{row}{n}") for row in "ABC" for n in range(1, 7)
    ]


def test_free_tenant_org_has_a_seated_event_guests_can_book_for_free(command: Command) -> None:
    """The seated tier is FREE so a guest hold -> checkout -> email confirmation completes without Stripe."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_ORG_SLUG)
    _assert_seated_event(
        org.events.get(slug="tenant-seated-event"), org, "Seated Free", TicketTier.PaymentMethod.FREE, 0
    )


def test_paid_tenant_org_has_an_online_seated_event(command: Command) -> None:
    """Guest ONLINE checkout of seated tiers is supported (reservation, then the Stripe session)."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_PAID_ORG_SLUG)
    _assert_seated_event(
        org.events.get(slug="tenant-paid-seated-event"), org, "Seated Standard", TicketTier.PaymentMethod.ONLINE, 10
    )


def test_domain_setup_org_is_enabled_for_custom_domains_without_one(command: Command) -> None:
    """The domain-setup org is public, owned by the tenant persona, gate open, and has no domain to start with."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_DOMAIN_SETUP_ORG_SLUG)
    assert org.owner == command.tenant_owner
    assert org.visibility == Organization.Visibility.PUBLIC
    assert org.custom_domain_enabled is True
    assert not org.domains.exists()
    assert org.custom_domain is None


def test_shared_fixture_orgs_get_no_custom_domain(command: Command) -> None:
    """``eligibility-test-org`` and Org Alpha must stay on the platform host: no domain, gate closed."""
    command._create_tenant_domains()

    for slug in ["eligibility-test-org", ORG_ALPHA_SLUG]:
        org = Organization.objects.get(slug=slug)
        assert not org.domains.exists()
        assert org.custom_domain_enabled is False
        assert org.custom_domain is None
    assert OrganizationDomain.objects.count() == 3


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
    assert not Organization.objects.filter(
        slug__in=[
            tenant_orgs.TENANT_ORG_SLUG,
            tenant_orgs.TENANT_PAID_ORG_SLUG,
            tenant_orgs.TENANT_DOMAIN_SETUP_ORG_SLUG,
        ]
    )
    assert {"event": "tenant_domains_skipped", "reason": "debug_disabled", "log_level": "warning"} in logs


def test_revoke_org_has_its_own_host_and_no_events(command: Command) -> None:
    """The kill-switch E2E flips this org's switch, so it must not share a host or events with the other journeys."""
    command._create_tenant_domains()

    org = Organization.objects.get(slug=tenant_orgs.TENANT_REVOKE_ORG_SLUG)
    assert org.custom_domain == "revoke.localhost"
    assert not org.events.exists()
