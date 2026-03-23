"""Tests for BatchTicketService._assert_purchasable_by via create_batch.

Validates the purchasability enforcement logic that controls who can buy
tickets from a tier based on the tier's `purchasable_by` setting and
`restrict_purchase_to_linked_invitations` flag.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    Organization,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db

# Shorthand alias used throughout the test class
PB = TicketTier.PurchasableBy


class TestPurchasableByPublic:
    """PUBLIC tier -- any authenticated user can purchase, no restrictions."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """An open event with a future start date."""
        return Event.objects.create(
            organization=organization,
            name="Public Tier Event",
            slug="public-tier-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def public_tier(self, event: Event) -> TicketTier:
        """A free PUBLIC tier."""
        return TicketTier.objects.create(
            event=event,
            name="GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.PUBLIC,
        )

    def test_any_user_can_purchase_public_tier(
        self,
        event: Event,
        public_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """Any authenticated user can purchase from a PUBLIC tier.

        PUBLIC tiers have no invitation or membership requirements.
        """
        # Arrange
        service = BatchTicketService(event, public_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Test")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].status == Ticket.TicketStatus.ACTIVE

    def test_guest_user_can_purchase_public_tier(
        self,
        event: Event,
        public_tier: TicketTier,
        django_user_model: type[RevelUser],
    ) -> None:
        """A guest user (anonymous-like) can purchase from a PUBLIC tier.

        Guest users are authenticated but have the `guest=True` flag.
        """
        # Arrange
        guest = django_user_model.objects.create_user(
            username="guest_user",
            email="guest@example.com",
            password="pass",
            guest=True,
        )
        service = BatchTicketService(event, public_tier, guest)
        items = [TicketPurchaseItem(guest_name="Guest Ticket")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1


class TestPurchasableByInvited:
    """INVITED tier -- only users with an EventInvitation can purchase."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """An open event with a future start date."""
        return Event.objects.create(
            organization=organization,
            name="Invited Tier Event",
            slug="invited-tier-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def invited_tier(self, event: Event) -> TicketTier:
        """A free INVITED tier with restrict_purchase_to_linked_invitations=False."""
        return TicketTier.objects.create(
            event=event,
            name="Invitee GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED,
            restrict_purchase_to_linked_invitations=False,
        )

    @pytest.fixture
    def restricted_tier(self, event: Event) -> TicketTier:
        """A free INVITED tier with restrict_purchase_to_linked_invitations=True."""
        return TicketTier.objects.create(
            event=event,
            name="VIP Invitee",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )

    def test_invited_user_can_purchase_unrestricted_tier(
        self,
        event: Event,
        invited_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited user can purchase from an INVITED tier (restrict=False).

        When restrict_purchase_to_linked_invitations is False, any invitation
        to the event is sufficient -- no tier link required.
        """
        # Arrange
        EventInvitation.objects.create(event=event, user=public_user)
        service = BatchTicketService(event, invited_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Invited Guest")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_uninvited_user_blocked_from_invited_tier(
        self,
        event: Event,
        invited_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """A user without an invitation is blocked from an INVITED tier.

        Even with restrict_purchase_to_linked_invitations=False, the user
        must have an EventInvitation record for the event.
        """
        # Arrange -- no invitation created for public_user
        service = BatchTicketService(event, invited_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Uninvited")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_invited_user_with_tier_link_can_purchase_restricted_tier(
        self,
        event: Event,
        restricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited user whose invitation links to the tier can purchase (restrict=True).

        When restrict_purchase_to_linked_invitations is True, the invitation's
        M2M `tiers` field must include this specific tier.
        """
        # Arrange
        invitation = EventInvitation.objects.create(event=event, user=public_user)
        invitation.tiers.add(restricted_tier)
        service = BatchTicketService(event, restricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="VIP Guest")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_invited_user_without_tier_link_blocked_from_restricted_tier(
        self,
        event: Event,
        restricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited user whose invitation does NOT link to the tier is blocked (restrict=True).

        The user has an invitation to the event, but the invitation's tiers M2M
        does not include this restricted tier.
        """
        # Arrange -- invitation exists but without a tier link
        EventInvitation.objects.create(event=event, user=public_user)
        service = BatchTicketService(event, restricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Wrong Tier")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_uninvited_user_blocked_from_restricted_tier(
        self,
        event: Event,
        restricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """A user with no invitation at all is blocked from a restricted INVITED tier."""
        # Arrange -- no invitation at all
        service = BatchTicketService(event, restricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="No Invitation")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_member_without_invitation_blocked_from_invited_tier(
        self,
        event: Event,
        invited_tier: TicketTier,
        member_user: RevelUser,
        organization_membership: OrganizationMember,
    ) -> None:
        """A member without an invitation is blocked from an INVITED-only tier.

        The INVITED purchasable_by does not grant access based on membership
        alone -- an invitation is required.
        """
        # Arrange -- member_user has membership via organization_membership fixture
        service = BatchTicketService(event, invited_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Member Without Invite")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_guest_user_blocked_from_invited_tier(
        self,
        event: Event,
        invited_tier: TicketTier,
        django_user_model: type[RevelUser],
    ) -> None:
        """A guest user (no invitation, no membership) is blocked from INVITED tier."""
        # Arrange
        guest = django_user_model.objects.create_user(
            username="guest_invited",
            email="guest_invited@example.com",
            password="pass",
            guest=True,
        )
        service = BatchTicketService(event, invited_tier, guest)
        items = [TicketPurchaseItem(guest_name="Guest Attempt")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403


class TestPurchasableByMembers:
    """MEMBERS tier -- only organization members can purchase."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """An open event with a future start date."""
        return Event.objects.create(
            organization=organization,
            name="Members Tier Event",
            slug="members-tier-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def members_tier(self, event: Event) -> TicketTier:
        """A free MEMBERS tier."""
        return TicketTier.objects.create(
            event=event,
            name="Members GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.MEMBERS,
        )

    def test_member_can_purchase_members_tier(
        self,
        event: Event,
        members_tier: TicketTier,
        member_user: RevelUser,
        organization_membership: OrganizationMember,
    ) -> None:
        """An active organization member can purchase from a MEMBERS tier."""
        # Arrange
        service = BatchTicketService(event, members_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Member Guest")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_non_member_blocked_from_members_tier(
        self,
        event: Event,
        members_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """A non-member user is blocked from purchasing a MEMBERS tier."""
        # Arrange
        service = BatchTicketService(event, members_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Non-Member")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_invited_user_without_membership_blocked_from_members_tier(
        self,
        event: Event,
        members_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited user who is not a member is blocked from a MEMBERS tier.

        The MEMBERS purchasable_by does not consider invitations at all.
        """
        # Arrange
        EventInvitation.objects.create(event=event, user=public_user)
        service = BatchTicketService(event, members_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Invited Non-Member")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_paused_member_blocked_from_members_tier(
        self,
        event: Event,
        members_tier: TicketTier,
        member_user: RevelUser,
        organization: Organization,
    ) -> None:
        """A paused member is not considered active and is blocked.

        The active_only() manager filter only returns ACTIVE memberships.
        """
        # Arrange
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )
        service = BatchTicketService(event, members_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Paused Member")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_guest_user_blocked_from_members_tier(
        self,
        event: Event,
        members_tier: TicketTier,
        django_user_model: type[RevelUser],
    ) -> None:
        """A guest user is blocked from a MEMBERS tier."""
        # Arrange
        guest = django_user_model.objects.create_user(
            username="guest_member",
            email="guest_member@example.com",
            password="pass",
            guest=True,
        )
        service = BatchTicketService(event, members_tier, guest)
        items = [TicketPurchaseItem(guest_name="Guest Attempt")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403


class TestPurchasableByInvitedAndMembers:
    """INVITED_AND_MEMBERS tier -- members OR invited users can purchase."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """An open event with a future start date."""
        return Event.objects.create(
            organization=organization,
            name="Invited-Members Event",
            slug="invited-members-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            max_tickets_per_user=10,
        )

    @pytest.fixture
    def unrestricted_tier(self, event: Event) -> TicketTier:
        """INVITED_AND_MEMBERS tier without tier-linked restriction."""
        return TicketTier.objects.create(
            event=event,
            name="Combined GA",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED_AND_MEMBERS,
            restrict_purchase_to_linked_invitations=False,
        )

    @pytest.fixture
    def restricted_tier(self, event: Event) -> TicketTier:
        """INVITED_AND_MEMBERS tier with tier-linked restriction."""
        return TicketTier.objects.create(
            event=event,
            name="Combined VIP",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED_AND_MEMBERS,
            restrict_purchase_to_linked_invitations=True,
        )

    def test_member_can_purchase_unrestricted_tier(
        self,
        event: Event,
        unrestricted_tier: TicketTier,
        member_user: RevelUser,
        organization_membership: OrganizationMember,
    ) -> None:
        """A member can purchase from INVITED_AND_MEMBERS tier (restrict=False)."""
        # Arrange
        service = BatchTicketService(event, unrestricted_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Member")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_invited_user_can_purchase_unrestricted_tier(
        self,
        event: Event,
        unrestricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited non-member can purchase from INVITED_AND_MEMBERS tier (restrict=False)."""
        # Arrange
        EventInvitation.objects.create(event=event, user=public_user)
        service = BatchTicketService(event, unrestricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Invited")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_member_can_purchase_restricted_tier_without_invitation(
        self,
        event: Event,
        restricted_tier: TicketTier,
        member_user: RevelUser,
        organization_membership: OrganizationMember,
    ) -> None:
        """A member can purchase from a restricted INVITED_AND_MEMBERS tier even without an invitation.

        The membership check is independent of the invitation check.
        restrict_purchase_to_linked_invitations only affects the invitation path,
        not the membership path.
        """
        # Arrange -- no invitation, only membership
        service = BatchTicketService(event, restricted_tier, member_user)
        items = [TicketPurchaseItem(guest_name="Member Only")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_invited_user_with_tier_link_can_purchase_restricted_tier(
        self,
        event: Event,
        restricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited user with the correct tier link can purchase (restrict=True)."""
        # Arrange
        invitation = EventInvitation.objects.create(event=event, user=public_user)
        invitation.tiers.add(restricted_tier)
        service = BatchTicketService(event, restricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Linked Invitee")]

        # Act
        result = service.create_batch(items)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 1

    def test_invited_user_without_tier_link_blocked_from_restricted_tier(
        self,
        event: Event,
        restricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """An invited non-member whose invitation lacks the tier link is blocked (restrict=True).

        The user has an invitation but it does not link to this restricted tier,
        and the user is not a member, so both paths fail.
        """
        # Arrange -- invitation without tier link, no membership
        EventInvitation.objects.create(event=event, user=public_user)
        service = BatchTicketService(event, restricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Wrong Tier")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_uninvited_non_member_blocked(
        self,
        event: Event,
        unrestricted_tier: TicketTier,
        public_user: RevelUser,
    ) -> None:
        """A user who is neither a member nor invited is blocked."""
        # Arrange
        service = BatchTicketService(event, unrestricted_tier, public_user)
        items = [TicketPurchaseItem(guest_name="Nobody")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_guest_user_blocked_from_invited_and_members_tier(
        self,
        event: Event,
        unrestricted_tier: TicketTier,
        django_user_model: type[RevelUser],
    ) -> None:
        """A guest user (no invitation, no membership) is blocked."""
        # Arrange
        guest = django_user_model.objects.create_user(
            username="guest_combo",
            email="guest_combo@example.com",
            password="pass",
            guest=True,
        )
        service = BatchTicketService(event, unrestricted_tier, guest)
        items = [TicketPurchaseItem(guest_name="Guest Attempt")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403


class TestPurchasableByEdgeCases:
    """Edge cases and cross-cutting scenarios for purchasability enforcement."""

    @pytest.fixture
    def event(self, organization: Organization) -> Event:
        """An open event with a future start date."""
        return Event.objects.create(
            organization=organization,
            name="Edge Case Event",
            slug="edge-case-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            max_tickets_per_user=10,
        )

    def test_invitation_linked_to_different_tier_does_not_grant_access(
        self,
        event: Event,
        public_user: RevelUser,
    ) -> None:
        """An invitation linked to tier A does not grant access to restricted tier B.

        When restrict_purchase_to_linked_invitations is True, the invitation
        must specifically link to the exact tier being purchased.
        """
        # Arrange
        tier_a = TicketTier.objects.create(
            event=event,
            name="Tier A",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )
        tier_b = TicketTier.objects.create(
            event=event,
            name="Tier B",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )
        invitation = EventInvitation.objects.create(event=event, user=public_user)
        invitation.tiers.add(tier_a)  # linked to A, not B

        service = BatchTicketService(event, tier_b, public_user)
        items = [TicketPurchaseItem(guest_name="Wrong Tier")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_invitation_linked_to_multiple_tiers_grants_access_to_each(
        self,
        event: Event,
        public_user: RevelUser,
    ) -> None:
        """An invitation linked to multiple tiers grants access to each of them.

        When the invitation's tiers M2M includes both tier A and tier B,
        the user can purchase from either one.
        """
        # Arrange
        tier_a = TicketTier.objects.create(
            event=event,
            name="Multi-Tier A",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )
        tier_b = TicketTier.objects.create(
            event=event,
            name="Multi-Tier B",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )
        invitation = EventInvitation.objects.create(event=event, user=public_user)
        invitation.tiers.add(tier_a, tier_b)

        # Act & Assert -- both tiers should succeed
        service_a = BatchTicketService(event, tier_a, public_user)
        result_a = service_a.create_batch([TicketPurchaseItem(guest_name="From A")])
        assert isinstance(result_a, list)

        service_b = BatchTicketService(event, tier_b, public_user)
        result_b = service_b.create_batch([TicketPurchaseItem(guest_name="From B")])
        assert isinstance(result_b, list)

    def test_banned_member_blocked_from_members_tier(
        self,
        event: Event,
        member_user: RevelUser,
        organization: Organization,
    ) -> None:
        """A banned member is not considered active and is blocked from MEMBERS tier.

        The active_only() manager filter only returns ACTIVE memberships.
        """
        # Arrange
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Members Only",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.MEMBERS,
        )
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="Banned")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_cancelled_member_blocked_from_members_tier(
        self,
        event: Event,
        member_user: RevelUser,
        organization: Organization,
    ) -> None:
        """A cancelled member is not considered active and is blocked."""
        # Arrange
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="Members Only Cancelled",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.MEMBERS,
        )
        service = BatchTicketService(event, tier, member_user)
        items = [TicketPurchaseItem(guest_name="Cancelled")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403

    def test_error_message_is_meaningful(
        self,
        event: Event,
        public_user: RevelUser,
    ) -> None:
        """The 403 error message should indicate the user is not allowed to purchase."""
        # Arrange
        tier = TicketTier.objects.create(
            event=event,
            name="Blocked Tier",
            price=Decimal("0"),
            payment_method=TicketTier.PaymentMethod.FREE,
            purchasable_by=PB.MEMBERS,
        )
        service = BatchTicketService(event, tier, public_user)
        items = [TicketPurchaseItem(guest_name="Blocked")]

        # Act & Assert
        with pytest.raises(HttpError) as exc_info:
            service.create_batch(items)
        assert exc_info.value.status_code == 403
        assert "not allowed to purchase" in str(exc_info.value.message).lower()
