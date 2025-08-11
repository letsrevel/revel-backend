import pytest
from django.contrib.auth.models import AnonymousUser

from accounts.models import RevelUser
from events.models import (
    DEFAULT_TICKET_TIER_NAME,
    Event,
    EventInvitation,
    OrganizationMember,
    OrganizationStaff,
    TicketTier,
)

pytestmark = pytest.mark.django_db


# --- Fixtures for Tiers on Different Event Types ---


@pytest.fixture
def public_tier_on_public_event(public_event: Event) -> TicketTier:
    """A PUBLIC tier on a PUBLIC event."""
    return TicketTier.objects.create(
        event=public_event,
        name="Public Tier on Public Event",
        visibility=TicketTier.Visibility.PUBLIC,
    )


@pytest.fixture
def member_tier_on_members_event(members_only_event: Event) -> TicketTier:
    """A MEMBERS_ONLY tier on a MEMBERS_ONLY event."""
    return TicketTier.objects.create(
        event=members_only_event,
        name="Member Tier on Member Event",
        visibility=TicketTier.Visibility.MEMBERS_ONLY,
    )


@pytest.fixture
def private_tier_on_private_event(private_event: Event) -> TicketTier:
    """A PRIVATE tier on a PRIVATE event."""
    return TicketTier.objects.create(
        event=private_event,
        name="Private Tier on Private Event",
        visibility=TicketTier.Visibility.PRIVATE,
    )


@pytest.fixture
def public_tier_on_private_event(private_event: Event) -> TicketTier:
    """A PUBLIC tier on a PRIVATE event. Should only be visible to invitees."""
    return TicketTier.objects.create(
        event=private_event,
        name="Public Tier on Private Event",
        visibility=TicketTier.Visibility.PUBLIC,
    )


@pytest.fixture
def invitation_to_private_event(public_user: RevelUser, private_event: Event) -> EventInvitation:
    """An invitation for a public user to a private event, without a specific tier."""
    return EventInvitation.objects.create(user=public_user, event=private_event)


class TestTicketTierForUserVisibility:
    """Test suite for the TicketTier.objects.for_user() method."""

    def test_visibility_for_anonymous_user(
        self,
        public_event: Event,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
    ) -> None:
        """Anonymous users should only see PUBLIC tiers on PUBLIC events."""
        user = AnonymousUser()
        visible_tiers = TicketTier.objects.for_user(user)

        # The public_event fixture has requires_ticket=True, so the signal creates
        # a "General Admission" tier. We also created `public_tier_on_public_event`.
        # Both are public and on a public event, so both should be visible.
        default_public_tier = TicketTier.objects.get(event=public_event, name=DEFAULT_TICKET_TIER_NAME)

        assert visible_tiers.count() == 2
        assert public_tier_on_public_event in visible_tiers
        assert default_public_tier in visible_tiers
        assert member_tier_on_members_event not in visible_tiers
        assert private_tier_on_private_event not in visible_tiers

    def test_visibility_for_public_user_not_invited(
        self,
        public_user: RevelUser,
        public_event: Event,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
    ) -> None:
        """A public (authenticated but non-member/non-invited) user sees the same as anonymous."""
        visible_tiers = TicketTier.objects.for_user(public_user)
        default_public_tier = TicketTier.objects.get(event=public_event, name=DEFAULT_TICKET_TIER_NAME)

        assert visible_tiers.count() == 2
        assert public_tier_on_public_event in visible_tiers
        assert default_public_tier in visible_tiers
        assert member_tier_on_members_event not in visible_tiers
        assert private_tier_on_private_event not in visible_tiers

    def test_visibility_for_invited_public_user(
        self,
        public_user: RevelUser,
        private_event: Event,
        invitation_to_private_event: EventInvitation,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
        public_tier_on_private_event: TicketTier,
    ) -> None:
        """A user invited to a private event can see that event's public and private tiers."""
        visible_tiers = TicketTier.objects.for_user(public_user)

        default_public_tier = TicketTier.objects.get(
            event=public_tier_on_public_event.event, name=DEFAULT_TICKET_TIER_NAME
        )
        default_private_tier = TicketTier.objects.get(event=private_event, name=DEFAULT_TICKET_TIER_NAME)

        # Sees public event's public tiers (2) + private event's tiers (3: default, private, public-on-private)
        assert visible_tiers.count() == 5
        assert public_tier_on_public_event in visible_tiers
        assert default_public_tier in visible_tiers
        assert private_tier_on_private_event in visible_tiers
        assert public_tier_on_private_event in visible_tiers
        assert default_private_tier in visible_tiers
        assert member_tier_on_members_event not in visible_tiers

    def test_visibility_for_member_user(
        self,
        member_user: RevelUser,
        public_event: Event,
        members_only_event: Event,
        organization_membership: OrganizationMember,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
    ) -> None:
        """A member user should see public and members-only tiers for events they can access."""
        visible_tiers = TicketTier.objects.for_user(member_user)

        # A member can see the public_event and the members_only_event.
        # They should see all tiers on those events that are either PUBLIC or MEMBERS_ONLY.
        default_public_tier = TicketTier.objects.get(event=public_event, name=DEFAULT_TICKET_TIER_NAME)
        default_member_tier = TicketTier.objects.get(event=members_only_event, name=DEFAULT_TICKET_TIER_NAME)

        # EXPECTED:
        # 1. public_tier_on_public_event (Public tier on Public event)
        # 2. default_public_tier (Public tier on Public event)
        # 3. member_tier_on_members_event (Member tier on Member event)
        # 4. default_member_tier (Public tier on Member event) -> This was the bug
        assert visible_tiers.count() == 4

        # Assertions
        assert public_tier_on_public_event in visible_tiers
        assert default_public_tier in visible_tiers
        assert member_tier_on_members_event in visible_tiers
        assert default_member_tier in visible_tiers

        # They should NOT see the private tier because they are not invited to the private event
        assert private_tier_on_private_event not in visible_tiers

    def test_visibility_for_staff_user(
        self,
        organization_staff_user: RevelUser,
        staff_member: OrganizationStaff,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
        public_tier_on_private_event: TicketTier,
    ) -> None:
        """Staff users should see all tiers for events in their organization."""
        visible_tiers = TicketTier.objects.for_user(organization_staff_user)

        # Staff can see all tiers from all events in their organization
        # public (2), members_only (2), private (3) = 7 total
        assert visible_tiers.count() == 7
        assert public_tier_on_public_event in visible_tiers
        assert member_tier_on_members_event in visible_tiers
        assert private_tier_on_private_event in visible_tiers
        assert public_tier_on_private_event in visible_tiers

    def test_visibility_for_owner_user(
        self,
        organization_owner_user: RevelUser,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
    ) -> None:
        """The organization owner should see all tiers for events in their org."""
        visible_tiers = TicketTier.objects.for_user(organization_owner_user)

        # Owner can see all tiers from all events in their organization
        # public (2), members_only (2), private (2) = 6 total
        assert visible_tiers.count() == 6
        assert public_tier_on_public_event in visible_tiers
        assert member_tier_on_members_event in visible_tiers
        assert private_tier_on_private_event in visible_tiers

    def test_visibility_for_superuser(
        self,
        superuser: RevelUser,
        public_tier_on_public_event: TicketTier,
        member_tier_on_members_event: TicketTier,
        private_tier_on_private_event: TicketTier,
    ) -> None:
        """A superuser should see all tiers regardless of organization or visibility."""
        visible_tiers = TicketTier.objects.for_user(superuser)

        # Superuser sees everything that exists in the test DB
        assert visible_tiers.count() == 6
        assert public_tier_on_public_event in visible_tiers
        assert member_tier_on_members_event in visible_tiers
        assert private_tier_on_private_event in visible_tiers
