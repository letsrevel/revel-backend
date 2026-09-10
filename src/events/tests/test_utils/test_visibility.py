"""Tests for the shared visibility id-gathering helpers in ``events/utils/visibility.py``."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.models import Event, EventInvitation, Organization, OrganizationMember, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def test_members_only_org_stays_visible_to_valid_member_when_another_member_is_cancelled(
    public_user: RevelUser, organization_owner_user: RevelUser
) -> None:
    """Another member's CANCELLED/BANNED row must not hide the org from a valid member.

    Regression: ``.filter(memberships__user=user).exclude(memberships__status__in=...)``
    excluded every organization that had *any* cancelled or banned membership.
    """
    org = Organization.objects.create(
        name="Members Only Org",
        slug="members-only-org",
        owner=organization_owner_user,
        visibility=Organization.Visibility.MEMBERS_ONLY,
    )
    OrganizationMember.objects.create(
        organization=org, user=public_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )
    other = RevelUser.objects.create_user(username="other-member", email="other@example.com", password="x")
    OrganizationMember.objects.create(
        organization=org, user=other, status=OrganizationMember.MembershipStatus.CANCELLED
    )

    assert org in Organization.objects.for_user(public_user)


def test_get_excluded_org_ids_unions_banned_and_blacklisted(
    public_user: RevelUser, organization_owner_user: RevelUser, organization: Organization
) -> None:
    from events.models import Blacklist
    from events.utils.visibility import get_excluded_org_ids

    banned_org = Organization.objects.create(name="Banned", slug="banned-org", owner=organization_owner_user)
    OrganizationMember.objects.create(
        organization=banned_org, user=public_user, status=OrganizationMember.MembershipStatus.BANNED
    )
    blacklisted_org = Organization.objects.create(name="Black", slug="blacklisted-org", owner=organization_owner_user)
    Blacklist.objects.create(organization=blacklisted_org, email=public_user.email, created_by=organization_owner_user)
    # A cancelled membership is not an exclusion.
    OrganizationMember.objects.create(
        organization=organization, user=public_user, status=OrganizationMember.MembershipStatus.CANCELLED
    )

    assert get_excluded_org_ids(public_user) == {banned_org.id, blacklisted_org.id}


def test_get_valid_member_org_ids_skips_cancelled_and_banned(
    public_user: RevelUser, organization_owner_user: RevelUser, organization: Organization
) -> None:
    from events.utils.visibility import get_valid_member_org_ids

    OrganizationMember.objects.create(organization=organization, user=public_user)  # ACTIVE
    paused_org = Organization.objects.create(name="Paused", slug="paused-org", owner=organization_owner_user)
    OrganizationMember.objects.create(
        organization=paused_org, user=public_user, status=OrganizationMember.MembershipStatus.PAUSED
    )
    for slug, status in (
        ("cancelled-org", OrganizationMember.MembershipStatus.CANCELLED),
        ("banned-org", OrganizationMember.MembershipStatus.BANNED),
    ):
        org = Organization.objects.create(name=slug, slug=slug, owner=organization_owner_user)
        OrganizationMember.objects.create(organization=org, user=public_user, status=status)

    assert set(get_valid_member_org_ids(public_user)) == {organization.id, paused_org.id}


def test_owner_or_staff_q_respects_org_lookup(
    organization_owner_user: RevelUser, organization_staff_user: RevelUser, public_user: RevelUser, event: Event
) -> None:
    from events.models import OrganizationStaff
    from events.utils.visibility import owner_or_staff_q

    OrganizationStaff.objects.create(organization=event.organization, user=organization_staff_user)

    assert Event.objects.filter(owner_or_staff_q(organization_owner_user)).filter(pk=event.pk).exists()
    assert Event.objects.filter(owner_or_staff_q(organization_staff_user)).filter(pk=event.pk).exists()
    assert not Event.objects.filter(owner_or_staff_q(public_user)).filter(pk=event.pk).exists()
    tier_q = owner_or_staff_q(organization_owner_user, org_lookup="event__organization")
    assert (
        TicketTier.objects.filter(tier_q).filter(event=event).exists()
        is TicketTier.objects.filter(event=event).exists()
    )


def test_get_ticketed_event_ids_cancelled_switch(
    member_user: RevelUser, event: Event, ticket_factory: t.Callable[..., Ticket]
) -> None:
    from events.utils.visibility import get_ticketed_event_ids

    ticket_factory(user=member_user, status=Ticket.TicketStatus.CANCELLED)

    assert set(get_ticketed_event_ids(member_user, include_cancelled=True)) == {event.id}
    assert set(get_ticketed_event_ids(member_user, include_cancelled=False)) == set()


def test_get_rsvp_event_ids_confirmed_switch(member_user: RevelUser, event: Event) -> None:
    from events.models import EventRSVP
    from events.utils.visibility import get_rsvp_event_ids

    EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.NO)

    assert set(get_rsvp_event_ids(member_user, confirmed_only=False)) == {event.id}
    assert set(get_rsvp_event_ids(member_user, confirmed_only=True)) == set()


def test_get_invited_event_ids(invitation: EventInvitation, public_user: RevelUser, member_user: RevelUser) -> None:
    from events.utils.visibility import get_invited_event_ids

    assert set(get_invited_event_ids(public_user)) == {invitation.event_id}
    assert set(get_invited_event_ids(member_user)) == set()
