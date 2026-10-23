# src/events/management/commands/bootstrap_helpers/tenant_orgs.py
"""Dedicated white-label tenant organizations for the frontend E2E suite (#1152).

The frontend 301s the platform URL of any organization with an active custom domain to
``https://{custom_domain}``, so custom domains must never sit on the shared fixture orgs the
other journeys visit on the platform host. These organizations exist only to own them.
"""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from django.conf import settings

from accounts.models import RevelUser
from events import models as events_models
from events.service import custom_domain_service
from geo.models import City

logger = structlog.get_logger(__name__)

TENANT_ORG_SLUG = "tenant-test-org"
TENANT_HOSTNAME = "tenant.localhost"
TENANT_RSVP_EVENT_SLUG = "tenant-rsvp-event"
TENANT_FREE_TICKET_EVENT_SLUG = "tenant-free-ticket-event"
TENANT_FREE_TIER_NAME = "Free Ticket"
TENANT_BRANDING = {"primary_color": "#B0306A", "accent_color": "#2E86DE"}

TENANT_PAID_ORG_SLUG = "tenant-paid-org"
TENANT_PAID_HOSTNAME = "paid.localhost"
TENANT_PAID_EVENT_SLUG = "tenant-paid-event"
TENANT_PAID_TIER_NAME = "Standard Ticket"
TENANT_PAID_BRANDING = {"primary_color": "#0E7C66", "accent_color": "#F2A541"}

TENANT_DOMAIN_SETUP_ORG_SLUG = "domain-setup-org"


def create_tenant_fixtures(owner: RevelUser, city: City, now: datetime) -> None:
    """Seed the tenant organizations: two with an ACTIVE ``*.localhost`` custom domain, one with none.

    ``*.localhost`` hostnames are only valid under DEBUG, so this is skipped (with a warning) otherwise.
    """
    if not settings.DEBUG:
        logger.warning("tenant_domains_skipped", reason="debug_disabled")
        return
    _create_free_org(owner, city, now)
    _create_paid_org(owner, city, now)
    _create_domain_setup_org(owner, city)


def _org(owner: RevelUser, city: City, name: str, slug: str, **extra: t.Any) -> events_models.Organization:
    return events_models.Organization.objects.create(
        name=name,
        slug=slug,
        owner=owner,
        visibility=events_models.Organization.Visibility.PUBLIC,
        description=f"{name}: white-label fixture organization for the custom-domain E2E journeys.",
        city=city,
        **extra,
    )


def _event(
    org: events_models.Organization, slug: str, name: str, start: datetime, **extra: t.Any
) -> events_models.Event:
    return events_models.Event.objects.create(
        organization=org,
        name=name,
        slug=slug,
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=org.city,
        start=start,
        end=start + timedelta(hours=3),
        address="Tenant Venue, Vienna, Austria",
        **extra,
    )


def _configure_default_tier(event: events_models.Event, **fields: t.Any) -> None:
    """Rename and configure the default tier the ``post_save`` signal created for a ticketed event."""
    events_models.TicketTier.objects.filter(event=event, name=events_models.DEFAULT_TICKET_TIER_NAME).update(
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        **fields,
    )


def _activate_domain(org: events_models.Organization, hostname: str, branding: dict[str, str]) -> None:
    """Brand ``org``, open its custom-domain gate and activate ``hostname`` for it."""
    org.branding = branding
    org.custom_domain_enabled = True
    org.save(update_fields=["branding", "custom_domain_enabled"])
    domain = events_models.OrganizationDomain.objects.create(organization=org, hostname=hostname)
    custom_domain_service.activate_domain(domain)
    logger.info(f"Activated custom domain {hostname} for {org.slug}")


def _create_free_org(owner: RevelUser, city: City, now: datetime) -> None:
    """Public org with one RSVP-able event and one free-ticket event, served from ``tenant.localhost``."""
    org = _org(owner, city, "Tenant Test Org", TENANT_ORG_SLUG)
    _event(
        org,
        TENANT_RSVP_EVENT_SLUG,
        "Tenant RSVP Event",
        now + timedelta(days=30),
        requires_ticket=False,
    )
    ticket_event = _event(
        org,
        TENANT_FREE_TICKET_EVENT_SLUG,
        "Tenant Free Ticket Event",
        now + timedelta(days=31),
        requires_ticket=True,
    )
    _configure_default_tier(
        ticket_event,
        name=TENANT_FREE_TIER_NAME,
        payment_method=events_models.TicketTier.PaymentMethod.FREE,
        price=Decimal("0.00"),
        currency="EUR",
        total_quantity=100,
    )
    _activate_domain(org, TENANT_HOSTNAME, TENANT_BRANDING)


def _platform_stripe_account() -> str | None:
    """The platform's own Stripe account id, if it is configured and no other organization holds it.

    ``stripe_account_id`` is unique. An organization on the platform account is charged directly on it
    (no connected-account parameters), which is what lets the E2E suite run real test-mode checkouts.
    """
    account: str = settings.STRIPE_ACCOUNT
    if not account.startswith("acct_"):
        logger.warning(
            "tenant_paid_org_without_stripe",
            reason="stripe_account_unset",
            detail="STRIPE_ACCOUNT is a placeholder: paid checkout on tenant-paid-org will fail.",
        )
        return None
    if events_models.Organization.objects.filter(stripe_account_id=account).exists():
        logger.warning(
            "tenant_paid_org_without_stripe",
            reason="stripe_account_in_use",
            detail="STRIPE_ACCOUNT belongs to another organization: paid checkout on tenant-paid-org will fail.",
        )
        return None
    return account


def _create_paid_org(owner: RevelUser, city: City, now: datetime) -> None:
    """Public org with one online paid event, served from ``paid.localhost``."""
    account = _platform_stripe_account()
    stripe_fields: dict[str, t.Any] = (
        {"stripe_account_id": account, "stripe_charges_enabled": True, "stripe_details_submitted": True}
        if account
        else {}
    )
    org = _org(owner, city, "Tenant Paid Org", TENANT_PAID_ORG_SLUG, **stripe_fields)
    event = _event(
        org,
        TENANT_PAID_EVENT_SLUG,
        "Tenant Paid Event",
        now + timedelta(days=32),
        requires_ticket=True,
    )
    _configure_default_tier(
        event,
        name=TENANT_PAID_TIER_NAME,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        price=Decimal("10.00"),
        currency="EUR",
        total_quantity=100,
    )
    _activate_domain(org, TENANT_PAID_HOSTNAME, TENANT_PAID_BRANDING)


def _create_domain_setup_org(owner: RevelUser, city: City) -> None:
    """Public org with the custom-domain gate open and no domain, so its owner can request one in the admin UI."""
    _org(owner, city, "Domain Setup Org", TENANT_DOMAIN_SETUP_ORG_SLUG, custom_domain_enabled=True)
    logger.info("domain_setup_org_created", slug=TENANT_DOMAIN_SETUP_ORG_SLUG)
