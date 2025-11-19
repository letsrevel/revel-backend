"""Tests for membership status impact on visibility (for_user methods)."""

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationMember, TicketTier

pytestmark = pytest.mark.django_db


# --- Organization Visibility Tests ---


def test_banned_user_cannot_see_public_organization(public_user: RevelUser, organization: Organization) -> None:
    """A banned user should not see even public organizations they are banned from."""
    # Create a banned membership
    OrganizationMember.objects.create(
        organization=organization, user=public_user, status=OrganizationMember.MembershipStatus.BANNED
    )

    # User should not see this organization
    orgs = Organization.objects.for_user(public_user)
    assert organization not in orgs


def test_cancelled_user_sees_public_organization(public_user: RevelUser, organization: Organization) -> None:
    """A cancelled user should still see public organizations (treated as non-member)."""
    # Ensure organization is public
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save()

    # Create a cancelled membership
    OrganizationMember.objects.create(
        organization=organization, user=public_user, status=OrganizationMember.MembershipStatus.CANCELLED
    )

    # User should see this public organization
    orgs = Organization.objects.for_user(public_user)
    assert organization in orgs


def test_paused_user_sees_public_organization(public_user: RevelUser, organization: Organization) -> None:
    """A paused user should still see public organizations."""
    # Create a paused membership
    OrganizationMember.objects.create(
        organization=organization, user=public_user, status=OrganizationMember.MembershipStatus.PAUSED
    )

    # User should see this public organization
    orgs = Organization.objects.for_user(public_user)
    assert organization in orgs


def test_cancelled_user_cannot_see_members_only_organization(
    public_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """A cancelled user should not see members-only organizations."""
    # Create a members-only organization
    members_only_org = Organization.objects.create(
        name="Members Only Org",
        slug="members-only-org",
        owner=organization_owner_user,
        visibility=Organization.Visibility.MEMBERS_ONLY,
    )

    # Create a cancelled membership
    OrganizationMember.objects.create(
        organization=members_only_org, user=public_user, status=OrganizationMember.MembershipStatus.CANCELLED
    )

    # User should not see this organization
    orgs = Organization.objects.for_user(public_user)
    assert members_only_org not in orgs


def test_paused_user_can_see_members_only_organization(
    public_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """A paused user should still see members-only organizations (visibility granted)."""
    # Create a members-only organization
    members_only_org = Organization.objects.create(
        name="Members Only Org",
        slug="members-only-org",
        owner=organization_owner_user,
        visibility=Organization.Visibility.MEMBERS_ONLY,
    )

    # Create a paused membership
    OrganizationMember.objects.create(
        organization=members_only_org, user=public_user, status=OrganizationMember.MembershipStatus.PAUSED
    )

    # User should see this organization
    orgs = Organization.objects.for_user(public_user)
    assert members_only_org in orgs


# --- Event Visibility Tests ---


def test_banned_user_cannot_see_public_event(public_user: RevelUser, public_event: Event) -> None:
    """A banned user should not see even public events from organizations they are banned from."""
    # Create a banned membership
    OrganizationMember.objects.create(
        organization=public_event.organization, user=public_user, status=OrganizationMember.MembershipStatus.BANNED
    )

    # User should not see this event
    events = Event.objects.for_user(public_user)
    assert public_event not in events


def test_cancelled_user_sees_public_event(public_user: RevelUser, public_event: Event) -> None:
    """A cancelled user should still see public events (treated as non-member)."""
    # Create a cancelled membership
    OrganizationMember.objects.create(
        organization=public_event.organization, user=public_user, status=OrganizationMember.MembershipStatus.CANCELLED
    )

    # User should see this public event
    events = Event.objects.for_user(public_user)
    assert public_event in events


def test_cancelled_user_cannot_see_members_only_event(public_user: RevelUser, members_only_event: Event) -> None:
    """A cancelled user should not see members-only events."""
    # Create a cancelled membership
    OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=public_user,
        status=OrganizationMember.MembershipStatus.CANCELLED,
    )

    # User should not see this event
    events = Event.objects.for_user(public_user)
    assert members_only_event not in events


def test_paused_user_can_see_members_only_event(public_user: RevelUser, members_only_event: Event) -> None:
    """A paused user should still see members-only events (visibility granted)."""
    # Create a paused membership
    OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=public_user,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    # User should see this event
    events = Event.objects.for_user(public_user)
    assert members_only_event in events


# --- TicketTier Visibility Tests ---


def test_banned_user_cannot_see_ticket_tiers(public_user: RevelUser, public_event: Event, vip_tier: TicketTier) -> None:
    """A banned user should not see ticket tiers from organizations they are banned from."""
    # Create a banned membership
    OrganizationMember.objects.create(
        organization=public_event.organization, user=public_user, status=OrganizationMember.MembershipStatus.BANNED
    )

    # User should not see this tier (because they can't see the event)
    tiers = TicketTier.objects.for_user(public_user)
    assert vip_tier not in tiers


def test_cancelled_user_cannot_see_members_only_ticket_tier(public_user: RevelUser, public_event: Event) -> None:
    """A cancelled user should not see members-only ticket tiers."""
    # Create a members-only tier
    members_tier = TicketTier.objects.create(
        event=public_event,
        name="Members Only Tier",
        visibility=TicketTier.Visibility.MEMBERS_ONLY,
        price=50.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create a cancelled membership
    OrganizationMember.objects.create(
        organization=public_event.organization, user=public_user, status=OrganizationMember.MembershipStatus.CANCELLED
    )

    # User should not see the members-only tier
    tiers = TicketTier.objects.for_user(public_user)
    assert members_tier not in tiers


def test_paused_user_can_see_members_only_ticket_tier(public_user: RevelUser, public_event: Event) -> None:
    """A paused user should see members-only ticket tiers (visibility granted)."""
    # Create a members-only tier
    members_tier = TicketTier.objects.create(
        event=public_event,
        name="Members Only Tier",
        visibility=TicketTier.Visibility.MEMBERS_ONLY,
        price=50.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create a paused membership
    OrganizationMember.objects.create(
        organization=public_event.organization, user=public_user, status=OrganizationMember.MembershipStatus.PAUSED
    )

    # User should see the members-only tier
    tiers = TicketTier.objects.for_user(public_user)
    assert members_tier in tiers
