# src/events/management/commands/bootstrap_helpers/organizations.py
"""Organization creation for bootstrap process."""

import structlog
from decouple import config

from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_organizations(state: BootstrapState) -> None:
    """Create multiple organizations with varied configurations."""
    logger.info("Creating organizations...")

    # Organization Alpha - Public organization with Stripe Connect
    org_alpha = events_models.Organization.objects.create(
        name="Revel Events Collective",
        slug="revel-events-collective",
        owner=state.users["org_alpha_owner"],
        visibility=events_models.Organization.Visibility.PUBLIC,
        description="""# Revel Events Collective

We're a vibrant community dedicated to bringing people together through unforgettable experiences.
From intimate gatherings to large-scale celebrations, we create events that spark joy, foster
connections, and celebrate life's special moments.

## Our Mission
To transform ordinary moments into extraordinary memories through thoughtfully curated events
that bring communities together.

## What We Do
- Music and cultural events
- Community workshops
- Seasonal celebrations
- Private gatherings
""",
        city=state.cities["vienna"],
        stripe_account_id=config("CONNECTED_TEST_STRIPE_ID", default=None),
        stripe_charges_enabled=True,
        stripe_details_submitted=True,
    )
    org_alpha.staff_members.add(state.users["org_alpha_staff"])

    # Add members with default tier
    default_tier_alpha = events_models.MembershipTier.objects.get(organization=org_alpha, name="General membership")
    for user in [state.users["org_alpha_member"], state.users["multi_org_user"]]:
        events_models.OrganizationMember.objects.create(organization=org_alpha, user=user, tier=default_tier_alpha)

    org_alpha.add_tags("community", "music", "arts")

    # Update organization settings
    org_alpha.accept_membership_requests = True
    org_alpha.contact_email = "hello@revelcollective.example.com"
    org_alpha.contact_email_verified = True
    org_alpha.save()

    state.orgs["alpha"] = org_alpha

    # Organization Beta - Members-only organization
    org_beta = events_models.Organization.objects.create(
        name="Tech Innovators Network",
        slug="tech-innovators-network",
        owner=state.users["org_beta_owner"],
        visibility=events_models.Organization.Visibility.PUBLIC,
        description="""# Tech Innovators Network

An exclusive community for tech professionals, entrepreneurs, and innovators. Join us for
cutting-edge workshops, networking events, and knowledge-sharing sessions.

## Membership Benefits
- Access to exclusive tech workshops and conferences
- Networking with industry leaders
- Early access to product launches and beta programs
- Members-only online resources and forums

## Join Us
Membership is by invitation or application review. We're looking for passionate technologists
who want to shape the future.
""",
        city=state.cities["berlin"],
    )
    org_beta.staff_members.add(state.users["org_beta_staff"])

    # Add members with default tier
    default_tier_beta = events_models.MembershipTier.objects.get(organization=org_beta, name="General membership")
    for user in [state.users["org_beta_member"], state.users["multi_org_user"], state.users["attendee_1"]]:
        events_models.OrganizationMember.objects.create(organization=org_beta, user=user, tier=default_tier_beta)

    org_beta.add_tags("tech", "professional", "networking")

    # Update organization settings
    org_beta.accept_membership_requests = True
    org_beta.contact_email = "info@techinnovators.example.com"
    org_beta.contact_email_verified = True
    org_beta.save()

    state.orgs["beta"] = org_beta

    logger.info(f"Created {len(state.orgs)} organizations")
