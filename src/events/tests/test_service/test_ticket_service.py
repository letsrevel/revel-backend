"""Tests for ticket_service functions, specifically get_eligible_tiers."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventInvitation,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
    TicketTier,
)
from events.service.ticket_service import get_eligible_tiers

pytestmark = pytest.mark.django_db


class TestGetEligibleTiersVisibility:
    """Tests for visibility logic in get_eligible_tiers."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def staff_user(self, revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
        """Organization staff member."""
        user = revel_user_factory(username="staff_user")
        OrganizationStaff.objects.create(
            organization=org,
            user=user,
            permissions=PermissionsSchema(default=PermissionMap()).model_dump(mode="json"),
        )
        return user

    @pytest.fixture
    def member_user(self, revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
        """Active organization member."""
        user = revel_user_factory(username="member_user")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def invited_user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """User who will be invited to events."""
        return revel_user_factory(username="invited_user")

    @pytest.fixture
    def regular_user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Regular user with no special status."""
        return revel_user_factory(username="regular_user")

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    @pytest.fixture
    def public_tier(self, event: Event) -> TicketTier:
        """PUBLIC visibility tier."""
        return TicketTier.objects.create(
            event=event,
            name="Public Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    @pytest.fixture
    def members_only_tier(self, event: Event) -> TicketTier:
        """MEMBERS_ONLY visibility tier."""
        return TicketTier.objects.create(
            event=event,
            name="Members Only Tier",
            visibility=TicketTier.Visibility.MEMBERS_ONLY,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    @pytest.fixture
    def private_tier(self, event: Event) -> TicketTier:
        """PRIVATE visibility tier."""
        return TicketTier.objects.create(
            event=event,
            name="Private Tier",
            visibility=TicketTier.Visibility.PRIVATE,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    @pytest.fixture
    def staff_only_tier(self, event: Event) -> TicketTier:
        """STAFF_ONLY visibility tier."""
        return TicketTier.objects.create(
            event=event,
            name="Staff Only Tier",
            visibility=TicketTier.Visibility.STAFF_ONLY,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    def test_regular_user_sees_only_public_tiers(
        self,
        event: Event,
        public_tier: TicketTier,
        members_only_tier: TicketTier,
        private_tier: TicketTier,
        staff_only_tier: TicketTier,
        regular_user: RevelUser,
    ) -> None:
        """Regular user should only see PUBLIC tiers."""
        eligible = get_eligible_tiers(event, regular_user)
        assert public_tier in eligible
        assert members_only_tier not in eligible
        assert private_tier not in eligible
        assert staff_only_tier not in eligible

    def test_member_sees_public_and_members_only_tiers(
        self,
        event: Event,
        public_tier: TicketTier,
        members_only_tier: TicketTier,
        private_tier: TicketTier,
        staff_only_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Organization member should see PUBLIC and MEMBERS_ONLY tiers."""
        eligible = get_eligible_tiers(event, member_user)
        assert public_tier in eligible
        assert members_only_tier in eligible
        assert private_tier not in eligible
        assert staff_only_tier not in eligible

    def test_invited_user_sees_public_and_private_tiers(
        self,
        event: Event,
        public_tier: TicketTier,
        members_only_tier: TicketTier,
        private_tier: TicketTier,
        staff_only_tier: TicketTier,
        invited_user: RevelUser,
    ) -> None:
        """Invited user should see PUBLIC and PRIVATE tiers."""
        EventInvitation.objects.create(event=event, user=invited_user)
        eligible = get_eligible_tiers(event, invited_user)
        assert public_tier in eligible
        assert members_only_tier not in eligible
        assert private_tier in eligible
        assert staff_only_tier not in eligible

    def test_staff_sees_all_tiers(
        self,
        event: Event,
        public_tier: TicketTier,
        members_only_tier: TicketTier,
        private_tier: TicketTier,
        staff_only_tier: TicketTier,
        staff_user: RevelUser,
    ) -> None:
        """Staff member should see all tiers."""
        eligible = get_eligible_tiers(event, staff_user)
        assert public_tier in eligible
        assert members_only_tier in eligible
        assert private_tier in eligible
        assert staff_only_tier in eligible

    def test_owner_sees_all_tiers(
        self,
        event: Event,
        public_tier: TicketTier,
        members_only_tier: TicketTier,
        private_tier: TicketTier,
        staff_only_tier: TicketTier,
        org_owner: RevelUser,
    ) -> None:
        """Organization owner should see all tiers."""
        eligible = get_eligible_tiers(event, org_owner)
        assert public_tier in eligible
        assert members_only_tier in eligible
        assert private_tier in eligible
        assert staff_only_tier in eligible


class TestGetEligibleTiersPurchasableBy:
    """Tests for purchasable_by logic in get_eligible_tiers."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def member_user(self, revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
        """Active organization member."""
        user = revel_user_factory(username="member_user")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def invited_user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """User who will be invited."""
        return revel_user_factory(username="invited_user")

    @pytest.fixture
    def regular_user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Regular user."""
        return revel_user_factory(username="regular_user")

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    def test_public_purchasable_by_allows_anyone(
        self,
        event: Event,
        regular_user: RevelUser,
    ) -> None:
        """PUBLIC purchasable_by should allow anyone."""
        tier = TicketTier.objects.create(
            event=event,
            name="Public Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        eligible = get_eligible_tiers(event, regular_user)
        assert tier in eligible

    def test_members_purchasable_by_requires_membership(
        self,
        event: Event,
        member_user: RevelUser,
        regular_user: RevelUser,
    ) -> None:
        """MEMBERS purchasable_by should only allow members."""
        tier = TicketTier.objects.create(
            event=event,
            name="Members Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Member can purchase
        eligible_member = get_eligible_tiers(event, member_user)
        assert tier in eligible_member

        # Regular user cannot
        eligible_regular = get_eligible_tiers(event, regular_user)
        assert tier not in eligible_regular

    def test_invited_purchasable_by_requires_invitation(
        self,
        event: Event,
        invited_user: RevelUser,
        regular_user: RevelUser,
    ) -> None:
        """INVITED purchasable_by should only allow invited users."""
        tier = TicketTier.objects.create(
            event=event,
            name="Invited Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        EventInvitation.objects.create(event=event, user=invited_user)

        # Invited user can purchase
        eligible_invited = get_eligible_tiers(event, invited_user)
        assert tier in eligible_invited

        # Regular user cannot
        eligible_regular = get_eligible_tiers(event, regular_user)
        assert tier not in eligible_regular

    def test_invited_and_members_purchasable_by(
        self,
        event: Event,
        member_user: RevelUser,
        invited_user: RevelUser,
        regular_user: RevelUser,
    ) -> None:
        """INVITED_AND_MEMBERS purchasable_by should allow both."""
        tier = TicketTier.objects.create(
            event=event,
            name="Invited and Members Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        EventInvitation.objects.create(event=event, user=invited_user)

        # Member can purchase
        assert tier in get_eligible_tiers(event, member_user)

        # Invited user can purchase
        assert tier in get_eligible_tiers(event, invited_user)

        # Regular user cannot
        assert tier not in get_eligible_tiers(event, regular_user)


class TestGetEligibleTiersSalesWindow:
    """Tests for sales window logic in get_eligible_tiers."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def regular_user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Regular user."""
        return revel_user_factory(username="regular_user")

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    def test_tier_before_sales_start_excluded(
        self,
        event: Event,
        regular_user: RevelUser,
    ) -> None:
        """Tier with sales window not yet started should be excluded."""
        tier = TicketTier.objects.create(
            event=event,
            name="Future Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_start_at=timezone.now() + timedelta(days=1),
        )
        eligible = get_eligible_tiers(event, regular_user)
        assert tier not in eligible

    def test_tier_after_sales_end_excluded(
        self,
        event: Event,
        regular_user: RevelUser,
    ) -> None:
        """Tier with sales window ended should be excluded."""
        tier = TicketTier.objects.create(
            event=event,
            name="Past Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_end_at=timezone.now() - timedelta(days=1),
        )
        eligible = get_eligible_tiers(event, regular_user)
        assert tier not in eligible

    def test_tier_within_sales_window_included(
        self,
        event: Event,
        regular_user: RevelUser,
    ) -> None:
        """Tier within sales window should be included."""
        tier = TicketTier.objects.create(
            event=event,
            name="Active Tier",
            visibility=TicketTier.Visibility.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_start_at=timezone.now() - timedelta(days=1),
            sales_end_at=timezone.now() + timedelta(days=1),
        )
        eligible = get_eligible_tiers(event, regular_user)
        assert tier in eligible


class TestGetEligibleTiersMembershipRestriction:
    """Tests for membership tier restriction logic in get_eligible_tiers."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def vip_membership_tier(self, org: Organization) -> MembershipTier:
        """VIP membership tier."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Membership",
        )

    @pytest.fixture
    def vip_member(
        self, revel_user_factory: RevelUserFactory, org: Organization, vip_membership_tier: MembershipTier
    ) -> RevelUser:
        """User with VIP membership tier."""
        user = revel_user_factory(username="vip_member")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            tier=vip_membership_tier,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def regular_member(self, revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
        """User with default (no specific tier) membership."""
        user = revel_user_factory(username="regular_member")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    def test_tier_restricted_to_specific_membership(
        self,
        event: Event,
        vip_membership_tier: MembershipTier,
        vip_member: RevelUser,
        regular_member: RevelUser,
    ) -> None:
        """Tier restricted to specific membership tier should only allow those members."""
        tier = TicketTier.objects.create(
            event=event,
            name="VIP Ticket",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        tier.restricted_to_membership_tiers.add(vip_membership_tier)

        # VIP member can see it
        assert tier in get_eligible_tiers(event, vip_member)

        # Regular member cannot (wrong membership tier)
        assert tier not in get_eligible_tiers(event, regular_member)

    def test_tier_without_membership_restriction_allows_any_member(
        self,
        event: Event,
        vip_member: RevelUser,
        regular_member: RevelUser,
    ) -> None:
        """Tier without membership restriction should allow any member."""
        tier = TicketTier.objects.create(
            event=event,
            name="Member Ticket",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # No restricted_to_membership_tiers set

        # Both members can see it
        assert tier in get_eligible_tiers(event, vip_member)
        assert tier in get_eligible_tiers(event, regular_member)


class TestGetEligibleTiersCombinedScenarios:
    """Tests for combined scenarios in get_eligible_tiers."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def member_user(self, revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
        """Active organization member."""
        user = revel_user_factory(username="member_user")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    def test_visible_but_not_purchasable_excluded(
        self,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Tier that is visible but not purchasable should be excluded.

        A tier can be PUBLIC visibility but INVITED purchasable_by, meaning
        users can see it but only invited users can buy.
        """
        tier = TicketTier.objects.create(
            event=event,
            name="Invite-Only Purchase",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # Member can see it (PUBLIC) but cannot purchase (INVITED only)
        eligible = get_eligible_tiers(event, member_user)
        assert tier not in eligible

    def test_multi_tier_event_returns_all_eligible(
        self,
        event: Event,
        member_user: RevelUser,
    ) -> None:
        """Event with multiple tiers should return all eligible ones."""
        tier1 = TicketTier.objects.create(
            event=event,
            name="General",
            visibility=TicketTier.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        tier2 = TicketTier.objects.create(
            event=event,
            name="Members Early Bird",
            visibility=TicketTier.Visibility.MEMBERS_ONLY,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        tier3 = TicketTier.objects.create(
            event=event,
            name="Invite Only",
            visibility=TicketTier.Visibility.PRIVATE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

        eligible = get_eligible_tiers(event, member_user)

        # Member sees tier1 (public) and tier2 (members only)
        assert tier1 in eligible
        assert tier2 in eligible
        # Not tier3 (not invited)
        assert tier3 not in eligible

    def test_empty_result_when_no_eligible_tiers(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Should return empty list when no tiers are eligible."""
        regular_user = revel_user_factory(username="nobody")

        # Delete auto-created tiers from signal
        event.ticket_tiers.all().delete()

        # All tiers are members-only or require invitation
        TicketTier.objects.create(
            event=event,
            name="Members Only",
            visibility=TicketTier.Visibility.MEMBERS_ONLY,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        TicketTier.objects.create(
            event=event,
            name="Invite Only",
            visibility=TicketTier.Visibility.PRIVATE,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

        eligible = get_eligible_tiers(event, regular_user)
        assert eligible == []


class TestGetEligibleTiersQueryOptimization:
    """Tests for query optimization in get_eligible_tiers."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def vip_membership_tier(self, org: Organization) -> MembershipTier:
        """VIP membership tier."""
        return MembershipTier.objects.create(
            organization=org,
            name="VIP Membership",
        )

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Test event."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )

    @pytest.fixture
    def member_user(self, revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
        """Active organization member."""
        user = revel_user_factory(username="member_user")
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        return user

    def test_no_n_plus_one_with_membership_restrictions(
        self,
        event: Event,
        vip_membership_tier: MembershipTier,
        member_user: RevelUser,
        django_assert_max_num_queries: t.Any,
    ) -> None:
        """Should not cause N+1 queries when checking membership tier restrictions.

        With 10 tiers each having membership restrictions, query count should be
        bounded (not 10+ queries for the restrictions).
        """
        # Delete auto-created tiers from signal
        event.ticket_tiers.all().delete()

        # Create 10 tiers with membership restrictions
        for i in range(10):
            tier = TicketTier.objects.create(
                event=event,
                name=f"Tier {i}",
                visibility=TicketTier.Visibility.PUBLIC,
                purchasable_by=TicketTier.PurchasableBy.MEMBERS,
                payment_method=TicketTier.PaymentMethod.FREE,
            )
            tier.restricted_to_membership_tiers.add(vip_membership_tier)

        # Expected queries (bounded, not N+1):
        # 1. Check if user is org owner
        # 2. Check if user is staff (staff_members filter)
        # 3. Get user membership
        # 4. Check if user has invitation
        # 5. Get tiers with prefetch_related for restricted_to_membership_tiers
        # = ~5 queries regardless of tier count
        with django_assert_max_num_queries(10):
            eligible = get_eligible_tiers(event, member_user)

        # Should still work correctly (member doesn't have VIP tier)
        assert len(eligible) == 0
