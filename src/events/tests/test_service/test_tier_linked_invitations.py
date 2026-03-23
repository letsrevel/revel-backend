"""Tests for tier-linked invitation restriction features.

Covers the two new TicketTier flags:
- restrict_visibility_to_linked_invitations (effective when visibility=PRIVATE)
- restrict_purchase_to_linked_invitations (effective when purchasable_by includes INVITED)

Also covers M2M tier links on EventInvitation (via direct invitations) and
EventToken (via token claiming).
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventInvitation,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
    TicketTier,
)
from events.models.mixins import VisibilityMixin
from events.schema import DirectInvitationCreateSchema
from events.service.invitation_service import create_direct_invitations
from events.service.ticket_service import (
    _check_purchasable_by,
    _check_tier_visibility,
    get_eligible_tiers,
)
from events.service.tokens import claim_invitation, create_event_token

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Organization owner."""
    return revel_user_factory(username="tli_owner")


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Test organization."""
    return Organization.objects.create(name="TLI Org", slug="tli-org", owner=owner)


@pytest.fixture
def staff_user(revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
    """Organization staff member."""
    user = revel_user_factory(username="tli_staff")
    OrganizationStaff.objects.create(
        organization=org,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap()).model_dump(mode="json"),
    )
    return user


@pytest.fixture
def member_user(revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
    """Active organization member."""
    user = revel_user_factory(username="tli_member")
    OrganizationMember.objects.create(
        organization=org,
        user=user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    return user


@pytest.fixture
def invited_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """User who will receive invitations."""
    return revel_user_factory(username="tli_invited")


@pytest.fixture
def regular_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """User with no relationship to the organization."""
    return revel_user_factory(username="tli_regular")


@pytest.fixture
def private_event(org: Organization) -> Event:
    """Private event requiring tickets, starting next week."""
    event = Event.objects.create(
        organization=org,
        name="TLI Private Event",
        slug="tli-private-event",
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
        requires_ticket=True,
    )
    # Delete auto-created "General Admission" tier to avoid noise in tests
    event.ticket_tiers.all().delete()
    return event


@pytest.fixture
def public_event(org: Organization) -> Event:
    """Public event requiring tickets, starting next week."""
    event = Event.objects.create(
        organization=org,
        name="TLI Public Event",
        slug="tli-public-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
        requires_ticket=True,
    )
    # Delete auto-created "General Admission" tier to avoid noise in tests
    event.ticket_tiers.all().delete()
    return event


# ---------------------------------------------------------------------------
# 1. Visibility restriction tests
# ---------------------------------------------------------------------------


class TestVisibilityRestriction:
    """Tests for restrict_visibility_to_linked_invitations on TicketTier."""

    def test_linked_invitation_can_see_restricted_tier(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """User with invitation linked to tier A CAN see tier A."""
        tier_a = TicketTier.objects.create(
            event=private_event,
            name="Tier A",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier_a)

        visible = TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier_a in visible

    def test_linked_to_A_cannot_see_restricted_tier_B(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """User with invitation linked to tier A CANNOT see tier B (also private+restricted)."""
        tier_a = TicketTier.objects.create(
            event=private_event,
            name="Tier A",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        tier_b = TicketTier.objects.create(
            event=private_event,
            name="Tier B",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier_a)

        visible_ids = set(
            TicketTier.objects.for_visible_event(private_event, invited_user).values_list("id", flat=True)
        )
        assert tier_a.id in visible_ids
        assert tier_b.id not in visible_ids

    def test_multi_tier_invitation_sees_all_linked(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """User with invitation linked to tiers A and B CAN see both."""
        tier_a = TicketTier.objects.create(
            event=private_event,
            name="Tier A",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        tier_b = TicketTier.objects.create(
            event=private_event,
            name="Tier B",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier_a, tier_b)

        visible = TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier_a in visible
        assert tier_b in visible

    def test_invitation_without_tier_links_cannot_see_restricted(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """User with invitation but no tier links CANNOT see restricted tiers."""
        TicketTier.objects.create(
            event=private_event,
            name="Restricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert TicketTier.objects.for_visible_event(private_event, invited_user).count() == 0

    def test_unrestricted_private_tier_visible_to_any_invited(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Unrestricted private tiers (flag=False) are visible to any invited user."""
        unrestricted = TicketTier.objects.create(
            event=private_event,
            name="Unrestricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=False,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert unrestricted in TicketTier.objects.for_visible_event(private_event, invited_user)

    def test_staff_sees_all_restricted_tiers(
        self,
        private_event: Event,
        staff_user: RevelUser,
    ) -> None:
        """Staff can see all tiers regardless of restriction flags."""
        restricted = TicketTier.objects.create(
            event=private_event,
            name="Restricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        assert restricted in TicketTier.objects.for_visible_event(private_event, staff_user)

    def test_owner_sees_all_restricted_tiers(
        self,
        private_event: Event,
        owner: RevelUser,
    ) -> None:
        """Organization owner can see all tiers regardless of restriction flags."""
        restricted = TicketTier.objects.create(
            event=private_event,
            name="Restricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        assert restricted in TicketTier.objects.for_visible_event(private_event, owner)

    def test_anonymous_cannot_see_private_tiers(self, private_event: Event) -> None:
        """Anonymous users cannot see private tiers."""
        TicketTier.objects.create(
            event=private_event,
            name="Private",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        assert TicketTier.objects.for_visible_event(private_event, AnonymousUser()).count() == 0

    def test_restriction_flag_rejected_for_public_visibility(
        self,
        public_event: Event,
    ) -> None:
        """Validation rejects restrict_visibility_to_linked_invitations on non-PRIVATE tiers."""
        with pytest.raises(DjangoValidationError):
            TicketTier.objects.create(
                event=public_event,
                name="Public",
                visibility=VisibilityMixin.Visibility.PUBLIC,
                restrict_visibility_to_linked_invitations=True,
            )

    def test_for_user_respects_restricted_visibility(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """TicketTierQuerySet.for_user also respects tier-linked restrictions."""
        tier_a = TicketTier.objects.create(
            event=private_event,
            name="A",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        tier_b = TicketTier.objects.create(
            event=private_event,
            name="B",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier_a)

        visible_ids = set(TicketTier.objects.for_user(invited_user).values_list("id", flat=True))
        assert tier_a.id in visible_ids
        assert tier_b.id not in visible_ids

    def test_mixed_restricted_and_unrestricted(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Restricted tier is hidden; unrestricted private tier remains visible."""
        restricted = TicketTier.objects.create(
            event=private_event,
            name="Restricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        unrestricted = TicketTier.objects.create(
            event=private_event,
            name="Unrestricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=False,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        visible_ids = set(
            TicketTier.objects.for_visible_event(private_event, invited_user).values_list("id", flat=True)
        )
        assert restricted.id not in visible_ids
        assert unrestricted.id in visible_ids


# ---------------------------------------------------------------------------
# 2. Purchase restriction tests
# ---------------------------------------------------------------------------


class TestPurchaseRestriction:
    """Tests for restrict_purchase_to_linked_invitations on TicketTier."""

    def test_linked_invited_user_can_purchase(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """User with invitation linked to tier CAN purchase."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="Purchase Tier",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier)

        assert tier in get_eligible_tiers(private_event, invited_user)

    def test_unlinked_invited_user_cannot_purchase(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """User with invitation NOT linked to tier CANNOT purchase."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="Purchase Tier",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
            restrict_visibility_to_linked_invitations=False,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert tier not in get_eligible_tiers(private_event, invited_user)

    def test_unrestricted_purchasable_by_any_invited(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Unrestricted tiers (flag=False) still purchasable by any invited user."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="Unrestricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=False,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert tier in get_eligible_tiers(private_event, invited_user)

    def test_invited_and_members_restricts_invited_allows_member(
        self,
        private_event: Event,
        invited_user: RevelUser,
        member_user: RevelUser,
    ) -> None:
        """INVITED_AND_MEMBERS: invited restricted by link, member CAN still purchase."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="Inv+Mem",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
            restrict_purchase_to_linked_invitations=True,
            restrict_visibility_to_linked_invitations=False,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)
        member_inv = EventInvitation.objects.create(event=private_event, user=member_user)
        member_inv.tiers.add(tier)

        assert tier not in get_eligible_tiers(private_event, invited_user)
        assert tier in get_eligible_tiers(private_event, member_user)

    def test_restriction_rejected_for_public_purchasable_by(
        self,
        public_event: Event,
    ) -> None:
        """Validation rejects restrict_purchase_to_linked_invitations on non-INVITED tiers."""
        with pytest.raises(DjangoValidationError):
            TicketTier.objects.create(
                event=public_event,
                name="Public Purch",
                visibility=VisibilityMixin.Visibility.PUBLIC,
                purchasable_by=TicketTier.PurchasableBy.PUBLIC,
                restrict_purchase_to_linked_invitations=True,
            )

    def test_restriction_rejected_for_members_purchasable_by(
        self,
        private_event: Event,
    ) -> None:
        """Validation rejects restrict_purchase_to_linked_invitations on MEMBERS-only tiers."""
        with pytest.raises(DjangoValidationError):
            TicketTier.objects.create(
                event=private_event,
                name="Mem Purch",
                visibility=VisibilityMixin.Visibility.PRIVATE,
                purchasable_by=TicketTier.PurchasableBy.MEMBERS,
                restrict_purchase_to_linked_invitations=True,
            )


# ---------------------------------------------------------------------------
# 3. Token claiming tests
# ---------------------------------------------------------------------------


class TestTokenClaimTierLinks:
    """Tests for tier links propagated from EventToken to EventInvitation on claim."""

    def test_claimed_invitation_gets_token_tiers(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        """When token has ticket_tiers, claimed invitation gets those tiers."""
        tier_a = TicketTier.objects.create(event=private_event, name="A")
        tier_b = TicketTier.objects.create(event=private_event, name="B")
        token = create_event_token(
            event=private_event,
            issuer=owner,
            duration=60,
            grants_invitation=True,
            ticket_tier_ids=[tier_a.id, tier_b.id],
        )

        invitation = claim_invitation(invited_user, token.pk)

        assert invitation is not None
        linked = set(invitation.tiers.values_list("id", flat=True))
        assert tier_a.id in linked
        assert tier_b.id in linked

    def test_claimed_invitation_has_no_tiers_when_token_has_none(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        """When token has no ticket_tiers, claimed invitation has no tiers."""
        token = create_event_token(
            event=private_event,
            issuer=owner,
            duration=60,
            grants_invitation=True,
            ticket_tier_ids=None,
        )

        invitation = claim_invitation(invited_user, token.pk)

        assert invitation is not None
        assert invitation.tiers.count() == 0

    def test_multiple_tiers_all_copied(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        """Multiple tiers on a token are all copied to the invitation."""
        tiers = [TicketTier.objects.create(event=private_event, name=f"T{i}") for i in range(3)]
        token = create_event_token(
            event=private_event,
            issuer=owner,
            duration=60,
            grants_invitation=True,
            ticket_tier_ids=[tier.id for tier in tiers],
        )

        invitation = claim_invitation(invited_user, token.pk)

        assert invitation is not None
        linked = set(invitation.tiers.values_list("id", flat=True))
        assert linked == {tier.id for tier in tiers}

    def test_create_event_token_stores_tier_ids(
        self,
        private_event: Event,
        owner: RevelUser,
    ) -> None:
        """create_event_token correctly links ticket tiers to the token."""
        tier = TicketTier.objects.create(event=private_event, name="TT")
        token = create_event_token(
            event=private_event,
            issuer=owner,
            duration=60,
            grants_invitation=True,
            ticket_tier_ids=[tier.id],
        )
        assert tier.id in set(token.ticket_tiers.values_list("id", flat=True))

    def test_create_event_token_rejects_other_event_tiers(
        self,
        private_event: Event,
        public_event: Event,
        owner: RevelUser,
    ) -> None:
        """create_event_token raises DoesNotExist for tiers not belonging to the event."""
        own = TicketTier.objects.create(event=private_event, name="Own")
        other = TicketTier.objects.create(event=public_event, name="Other")
        with pytest.raises(TicketTier.DoesNotExist):
            create_event_token(
                event=private_event,
                issuer=owner,
                duration=60,
                grants_invitation=True,
                ticket_tier_ids=[own.id, other.id],
            )

    def test_claim_adds_tiers_to_existing_invitation(
        self,
        private_event: Event,
        owner: RevelUser,
        invited_user: RevelUser,
    ) -> None:
        """Claiming a token with tiers adds them to an already-existing invitation."""
        tier_a = TicketTier.objects.create(event=private_event, name="A")
        tier_b = TicketTier.objects.create(event=private_event, name="B")
        # Pre-existing invitation with tier_a
        invitation = EventInvitation.objects.create(event=private_event, user=invited_user)
        invitation.tiers.add(tier_a)
        # Token grants tier_b
        token = create_event_token(
            event=private_event,
            issuer=owner,
            duration=60,
            grants_invitation=True,
            ticket_tier_ids=[tier_b.id],
        )

        result = claim_invitation(invited_user, token.pk)

        assert result is not None
        linked = set(result.tiers.values_list("id", flat=True))
        assert tier_a.id in linked  # existing tier kept
        assert tier_b.id in linked  # new tier added


# ---------------------------------------------------------------------------
# 4. Direct invitation tests
# ---------------------------------------------------------------------------


class TestDirectInvitationTierLinks:
    """Tests for tier links when creating direct invitations."""

    def test_links_specified_tiers(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Creating invitation with tier_ids links the tiers."""
        tier = TicketTier.objects.create(event=private_event, name="DT")
        schema = DirectInvitationCreateSchema(emails=[invited_user.email], tier_ids=[tier.id])

        result = create_direct_invitations(private_event, schema)

        assert result["created_invitations"] == 1
        inv = EventInvitation.objects.get(event=private_event, user=invited_user)
        assert tier.id in set(inv.tiers.values_list("id", flat=True))

    def test_empty_tier_ids_creates_invitation_with_no_tiers(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Creating invitation with empty tier_ids has no tiers."""
        schema = DirectInvitationCreateSchema(emails=[invited_user.email], tier_ids=[])

        create_direct_invitations(private_event, schema)

        inv = EventInvitation.objects.get(event=private_event, user=invited_user)
        assert inv.tiers.count() == 0

    def test_links_multiple_tiers(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Creating invitation with multiple tier_ids links all tiers."""
        tier_a = TicketTier.objects.create(event=private_event, name="DA")
        tier_b = TicketTier.objects.create(event=private_event, name="DB")
        schema = DirectInvitationCreateSchema(
            emails=[invited_user.email],
            tier_ids=[tier_a.id, tier_b.id],
        )

        create_direct_invitations(private_event, schema)

        inv = EventInvitation.objects.get(event=private_event, user=invited_user)
        linked = set(inv.tiers.values_list("id", flat=True))
        assert tier_a.id in linked
        assert tier_b.id in linked

    def test_invalid_tier_ids_raises(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Creating invitation with nonexistent tier_ids raises DoesNotExist."""
        schema = DirectInvitationCreateSchema(
            emails=[invited_user.email],
            tier_ids=[uuid.uuid4()],
        )
        with pytest.raises(TicketTier.DoesNotExist):
            create_direct_invitations(private_event, schema)


# ---------------------------------------------------------------------------
# 5. Both flags independent
# ---------------------------------------------------------------------------


class TestBothFlagsIndependent:
    """Tests that restrict_visibility and restrict_purchase operate independently."""

    def test_visibility_restricted_linked_user_sees_and_buys(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """restrict_visibility=True, restrict_purchase=False: linked user sees and buys."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="VisOnly",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_visibility_to_linked_invitations=True,
            restrict_purchase_to_linked_invitations=False,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier)

        assert tier in TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier in get_eligible_tiers(private_event, invited_user)

    def test_visibility_restricted_unlinked_user_hidden(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """restrict_visibility=True, restrict_purchase=False: unlinked user cannot see."""
        TicketTier.objects.create(
            event=private_event,
            name="VisOnly",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_visibility_to_linked_invitations=True,
            restrict_purchase_to_linked_invitations=False,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert TicketTier.objects.for_visible_event(private_event, invited_user).count() == 0

    def test_purchase_restricted_linked_user_sees_and_buys(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """restrict_visibility=False, restrict_purchase=True: linked user sees and buys."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="PurchOnly",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_visibility_to_linked_invitations=False,
            restrict_purchase_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier)

        assert tier in TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier in get_eligible_tiers(private_event, invited_user)

    def test_purchase_restricted_unlinked_sees_but_cannot_buy(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """restrict_visibility=False, restrict_purchase=True: unlinked sees but cannot buy."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="PurchOnly",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_visibility_to_linked_invitations=False,
            restrict_purchase_to_linked_invitations=True,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert tier in TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier not in get_eligible_tiers(private_event, invited_user)

    def test_both_true_linked_user_sees_and_buys(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Both flags True: linked user can see AND buy."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="BothRestricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_visibility_to_linked_invitations=True,
            restrict_purchase_to_linked_invitations=True,
        )
        inv = EventInvitation.objects.create(event=private_event, user=invited_user)
        inv.tiers.add(tier)

        assert tier in TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier in get_eligible_tiers(private_event, invited_user)

    def test_both_true_unlinked_user_cannot_see_or_buy(
        self,
        private_event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Both flags True: unlinked user cannot see OR buy."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="BothRestricted",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_visibility_to_linked_invitations=True,
            restrict_purchase_to_linked_invitations=True,
        )
        EventInvitation.objects.create(event=private_event, user=invited_user)

        assert tier not in TicketTier.objects.for_visible_event(private_event, invited_user)
        assert tier not in get_eligible_tiers(private_event, invited_user)


# ---------------------------------------------------------------------------
# 6. Unit tests for _check_tier_visibility and _check_purchasable_by
# ---------------------------------------------------------------------------


class TestCheckTierVisibilityHelper:
    """Unit tests for _check_tier_visibility."""

    def test_staff_always_passes(self, private_event: Event) -> None:
        """Staff/owners can see any tier regardless of restrictions."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        assert _check_tier_visibility(tier, True, False, False, set())

    def test_public_tier_always_visible(self, public_event: Event) -> None:
        """PUBLIC visibility is always visible (restriction flag not set)."""
        tier = TicketTier.objects.create(
            event=public_event,
            name="T",
            visibility=VisibilityMixin.Visibility.PUBLIC,
        )
        assert _check_tier_visibility(tier, False, False, False, set())

    def test_private_restricted_needs_tier_link(self, private_event: Event) -> None:
        """PRIVATE + restricted: needs tier ID in invitation_tier_ids."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
        )
        assert _check_tier_visibility(tier, False, False, True, {tier.id})
        assert not _check_tier_visibility(tier, False, False, True, set())

    def test_private_unrestricted_needs_invitation_only(self, private_event: Event) -> None:
        """PRIVATE + unrestricted: needs is_invited only."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            visibility=VisibilityMixin.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=False,
        )
        assert _check_tier_visibility(tier, False, False, True, set())
        assert not _check_tier_visibility(tier, False, False, False, set())

    def test_members_only(self, private_event: Event) -> None:
        """MEMBERS_ONLY requires membership."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            visibility=VisibilityMixin.Visibility.MEMBERS_ONLY,
        )
        assert _check_tier_visibility(tier, False, True, False, set())
        assert not _check_tier_visibility(tier, False, False, True, set())


class TestCheckPurchasableByHelper:
    """Unit tests for _check_purchasable_by."""

    def test_public_always_passes(self, public_event: Event) -> None:
        """PUBLIC purchasable_by allows anyone."""
        tier = TicketTier.objects.create(
            event=public_event,
            name="T",
            purchasable_by=TicketTier.PurchasableBy.PUBLIC,
        )
        assert _check_purchasable_by(tier, False, False, set())

    def test_invited_restricted_needs_link(self, private_event: Event) -> None:
        """INVITED + restricted: needs tier link."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
        )
        assert _check_purchasable_by(tier, False, True, {tier.id})
        assert not _check_purchasable_by(tier, False, True, set())

    def test_invited_unrestricted_needs_invitation_only(self, private_event: Event) -> None:
        """INVITED + unrestricted: any invited user passes."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=False,
        )
        assert _check_purchasable_by(tier, False, True, set())
        assert not _check_purchasable_by(tier, False, False, set())

    def test_invited_and_members_member_always_passes(self, private_event: Event) -> None:
        """INVITED_AND_MEMBERS: member passes even with purchase restriction."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            purchasable_by=TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
            restrict_purchase_to_linked_invitations=True,
        )
        assert _check_purchasable_by(tier, True, False, set())

    def test_invited_and_members_invited_needs_link_when_restricted(
        self,
        private_event: Event,
    ) -> None:
        """INVITED_AND_MEMBERS + restricted: invited needs link."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            purchasable_by=TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
            restrict_purchase_to_linked_invitations=True,
        )
        assert not _check_purchasable_by(tier, False, True, set())
        assert _check_purchasable_by(tier, False, True, {tier.id})

    def test_members_without_restriction_flag(self, private_event: Event) -> None:
        """MEMBERS purchasable_by: member passes, non-member fails."""
        tier = TicketTier.objects.create(
            event=private_event,
            name="T",
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
        )
        assert _check_purchasable_by(tier, True, False, set())
        assert not _check_purchasable_by(tier, False, True, {tier.id})
