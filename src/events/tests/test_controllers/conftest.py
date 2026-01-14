import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
)


@pytest.fixture
def superuser(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A superuser."""
    return django_user_model.objects.create_superuser(username="super", email="super@example.com", password="pass")


@pytest.fixture
def superuser_client(superuser: RevelUser) -> Client:
    """API client for a superuser."""
    refresh = RefreshToken.for_user(superuser)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for an organization owner."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_staff_client(organization_staff_user: RevelUser, staff_member: OrganizationStaff) -> Client:
    """API client for an organization staff member."""
    refresh = RefreshToken.for_user(organization_staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def member_client(member_user: RevelUser, organization: Organization) -> Client:
    """API client for a standard organization member."""
    OrganizationMember.objects.create(organization=organization, user=member_user)
    refresh = RefreshToken.for_user(member_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def nonmember_client(nonmember_user: RevelUser) -> Client:
    """API client for an authenticated user with no specific org relationship."""
    refresh = RefreshToken.for_user(nonmember_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# Dashboard test fixtures


@pytest.fixture
def dashboard_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A user for whom we'll test the dashboard."""
    return django_user_model.objects.create_user(username="dash", email="dash@example.com", password="p")


@pytest.fixture
def dashboard_client(dashboard_user: RevelUser) -> Client:
    """An authenticated client for the dashboard user."""
    refresh = RefreshToken.for_user(dashboard_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def dashboard_setup(dashboard_user: RevelUser) -> dict[str, t.Any]:
    """Creates a complex set of related objects for dashboard testing."""
    # Orgs with direct relationship
    org_owner = Organization.objects.create(name="Owned Org", owner=dashboard_user)
    org_staff = Organization.objects.create(name="Staff Org", owner=RevelUser.objects.create_user("anotherowner"))
    OrganizationStaff.objects.create(organization=org_staff, user=dashboard_user)
    org_member = Organization.objects.create(name="Member Org", owner=RevelUser.objects.create_user("thirdowner"))
    OrganizationMember.objects.create(organization=org_member, user=dashboard_user)

    # Orgs with indirect (event/sub) relationship
    org_public_rsvp = Organization.objects.create(
        name="RSVP Org", owner=RevelUser.objects.create_user("fourthowner"), visibility="public"
    )
    org_public_ticket = Organization.objects.create(
        name="Ticket Org", owner=RevelUser.objects.create_user("fifthowner"), visibility="public"
    )

    # A private org the user has no access to
    org_private_unrelated = Organization.objects.create(
        name="Unrelated Private Org", owner=RevelUser.objects.create_user("seventhowner"), visibility="private"
    )

    # Events
    evt_owner = Event.objects.create(name="In Owned Org", organization=org_owner, status="open", start=timezone.now())
    evt_staff = Event.objects.create(name="In Staff Org", organization=org_staff, status="open", start=timezone.now())
    evt_member = Event.objects.create(
        name="In Member Org",
        organization=org_member,
        status="open",
        visibility=Event.Visibility.MEMBERS_ONLY,
        start=timezone.now(),
    )
    evt_rsvp = Event.objects.create(
        name="RSVP'd Event", organization=org_public_rsvp, status="open", start=timezone.now()
    )
    EventRSVP.objects.create(event=evt_rsvp, user=dashboard_user, status="yes")
    evt_ticket = Event.objects.create(
        name="Ticketed Event", organization=org_public_ticket, status="open", start=timezone.now()
    )
    tier = evt_ticket.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(guest_name="Test Guest", event=evt_ticket, user=dashboard_user, tier=tier)
    evt_invite = Event.objects.create(
        name="Invited Event", organization=org_public_ticket, status="open", start=timezone.now()
    )  # another event in a public org
    EventInvitation.objects.create(event=evt_invite, user=dashboard_user)
    # This event is in a private org and user has no relation, so it shouldn't appear
    Event.objects.create(
        name="Unrelated Private Event", organization=org_private_unrelated, status="open", start=timezone.now()
    )

    return {
        "user": dashboard_user,
        "orgs": {
            "owner": org_owner,
            "staff": org_staff,
            "member": org_member,
            "rsvp": org_public_rsvp,
            "ticket": org_public_ticket,
            "private": org_private_unrelated,
        },
        "events": {
            "owner": evt_owner,
            "staff": evt_staff,
            "member": evt_member,
            "rsvp": evt_rsvp,
            "ticket": evt_ticket,
            "invite": evt_invite,
        },
    }
