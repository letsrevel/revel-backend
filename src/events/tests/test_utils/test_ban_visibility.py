"""A ban or hard blacklist overrides every ``for_user()`` visibility branch (#960).

Before #960 the banned/blacklisted organizations were only subtracted from the
*public* branch, so membership, staff, invitation, RSVP, ticket and token
branches still leaked the organization and its events. The rule now is: ban
trumps everything, except (a) an organization the user owns, and (b) in
``EventQuerySet.for_user``, an event the user holds a ticket for.
"""

import pytest

from accounts.models import RevelUser
from events.models import (
    AdditionalResource,
    Blacklist,
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
    TicketTier,
)
from events.models.mixins import ResourceVisibility

pytestmark = pytest.mark.django_db


def _blacklist(org: Organization, user: RevelUser) -> None:
    """Hard-blacklist ``user`` by email (independent of any membership row)."""
    Blacklist.objects.create(organization=org, email=user.email, created_by=org.owner)


def _ban(org: Organization, user: RevelUser) -> None:
    OrganizationMember.objects.update_or_create(
        organization=org, user=user, defaults={"status": OrganizationMember.MembershipStatus.BANNED}
    )


# --- Organization.for_user ---


def test_org_hidden_from_blacklisted_active_member(organization: Organization, member_user: RevelUser) -> None:
    organization.visibility = Organization.Visibility.MEMBERS_ONLY
    organization.save(update_fields=["visibility"])
    OrganizationMember.objects.create(organization=organization, user=member_user)
    assert organization in Organization.objects.for_user(member_user)

    _blacklist(organization, member_user)

    assert organization not in Organization.objects.for_user(member_user)


def test_org_hidden_from_banned_staff(organization: Organization, organization_staff_user: RevelUser) -> None:
    OrganizationStaff.objects.create(organization=organization, user=organization_staff_user)
    _ban(organization, organization_staff_user)

    assert organization not in Organization.objects.for_user(organization_staff_user)


def test_org_allowed_ids_do_not_bypass_ban(organization: Organization, public_user: RevelUser) -> None:
    _ban(organization, public_user)

    assert organization not in Organization.objects.for_user(public_user, allowed_ids=[organization.id])


def test_org_still_visible_to_owner_matched_by_blacklist(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """``add_to_blacklist`` stores rows matching the owner; they must not lock the owner out."""
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save(update_fields=["visibility"])
    _blacklist(organization, organization_owner_user)

    assert organization in Organization.objects.for_user(organization_owner_user)


# --- Event.for_user ---


def test_event_invitation_does_not_survive_ban(
    invitation: EventInvitation, private_event: Event, public_user: RevelUser
) -> None:
    assert private_event in Event.objects.for_user(public_user)

    _ban(private_event.organization, public_user)

    assert private_event not in Event.objects.for_user(public_user)


def test_event_rsvp_and_membership_do_not_survive_blacklist(members_only_event: Event, member_user: RevelUser) -> None:
    OrganizationMember.objects.create(organization=members_only_event.organization, user=member_user)
    EventRSVP.objects.create(event=members_only_event, user=member_user, status=EventRSVP.RsvpStatus.YES)
    assert members_only_event in Event.objects.for_user(member_user)

    _blacklist(members_only_event.organization, member_user)

    assert members_only_event not in Event.objects.for_user(member_user)


def test_event_staff_branch_does_not_survive_ban(private_event: Event, organization_staff_user: RevelUser) -> None:
    OrganizationStaff.objects.create(organization=private_event.organization, user=organization_staff_user)
    _ban(private_event.organization, organization_staff_user)

    assert private_event not in Event.objects.for_user(organization_staff_user)


def test_event_allowed_ids_do_not_bypass_ban(private_event: Event, public_user: RevelUser) -> None:
    _ban(private_event.organization, public_user)

    assert private_event not in Event.objects.for_user(public_user, allowed_ids=[private_event.id])


@pytest.mark.parametrize("status", [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CANCELLED])
def test_event_ticket_holder_keeps_event_after_blacklist(
    private_event: Event, public_event: Event, public_user: RevelUser, status: Ticket.TicketStatus
) -> None:
    """Ticket carve-out: the holder can still open the event their ticket/refund refers to."""
    tier = TicketTier.objects.create(event=private_event, name="GA")
    Ticket.objects.create(event=private_event, tier=tier, user=public_user, guest_name="x", status=status)

    _blacklist(private_event.organization, public_user)

    visible = Event.objects.for_user(public_user)
    assert private_event in visible
    # The carve-out is per event, not per organization.
    assert public_event not in visible


def test_event_still_visible_to_owner_matched_by_blacklist(
    private_event: Event, organization_owner_user: RevelUser
) -> None:
    _blacklist(private_event.organization, organization_owner_user)

    assert private_event in Event.objects.for_user(organization_owner_user)


# --- Downstream querysets (transitive) ---


def test_ticket_tiers_hidden_after_invitation_then_ban(
    invitation: EventInvitation, private_event: Event, public_user: RevelUser
) -> None:
    tier = TicketTier.objects.create(event=private_event, name="Invite tier")
    assert tier in TicketTier.objects.for_user(public_user)

    _ban(private_event.organization, public_user)

    assert tier not in TicketTier.objects.for_user(public_user)


def test_attendee_resources_hidden_from_blacklisted_ticket_holder(
    event: Event, event_ticket_tier: TicketTier, ticket: Ticket, member_user: RevelUser
) -> None:
    """The event stays reachable via the ticket, but its gated resources do not."""
    resource = AdditionalResource.objects.create(
        organization=event.organization,
        name="Attendee notes",
        visibility=ResourceVisibility.ATTENDEES_ONLY,
        resource_type=AdditionalResource.ResourceTypes.LINK,
        link="https://example.com",
    )
    resource.events.add(event)
    assert resource in AdditionalResource.objects.for_user(member_user)

    _blacklist(event.organization, member_user)

    assert resource not in AdditionalResource.objects.for_user(member_user)
    assert event in Event.objects.for_user(member_user, include_past=True)


def test_resources_still_visible_to_owner_matched_by_blacklist(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    resource = AdditionalResource.objects.create(
        organization=organization,
        name="Staff notes",
        visibility=ResourceVisibility.STAFF_ONLY,
        resource_type=AdditionalResource.ResourceTypes.LINK,
        link="https://example.com",
    )
    _blacklist(organization, organization_owner_user)

    assert resource in AdditionalResource.objects.for_user(organization_owner_user)
