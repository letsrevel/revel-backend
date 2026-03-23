"""Tests for can_purchase flag on tier listing and user event status.

Verifies that:
- list_tiers returns can_purchase=False for visible-but-not-eligible tiers
- get_user_event_status includes non-eligible tiers with can_purchase=False
- Staff/owners are exempt from purchasable_by in _assert_purchasable_by
"""

from datetime import timedelta

import pytest
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
    Ticket,
    TicketTier,
)
from events.models.mixins import VisibilityMixin
from events.service.ticket_service import UserEventStatus, get_user_event_status

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Organization owner."""
    return revel_user_factory(username="tcp_owner")


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Test organization."""
    return Organization.objects.create(name="TCP Org", slug="tcp-org", owner=owner)


@pytest.fixture
def staff_user(revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
    """Organization staff member."""
    user = revel_user_factory(username="tcp_staff")
    OrganizationStaff.objects.create(
        organization=org,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap()).model_dump(mode="json"),
    )
    return user


@pytest.fixture
def member_user(revel_user_factory: RevelUserFactory, org: Organization) -> RevelUser:
    """Active organization member."""
    user = revel_user_factory(username="tcp_member")
    OrganizationMember.objects.create(
        organization=org,
        user=user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    return user


@pytest.fixture
def invited_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """User who will receive invitations."""
    return revel_user_factory(username="tcp_invited")


@pytest.fixture
def event(org: Organization) -> Event:
    """Public event requiring tickets."""
    ev = Event.objects.create(
        organization=org,
        name="TCP Event",
        slug="tcp-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
        requires_ticket=True,
    )
    ev.ticket_tiers.all().delete()
    return ev


def _give_user_a_ticket(event: Event, user: RevelUser) -> Ticket:
    """Create a free tier + active ticket so get_user_event_status returns UserEventStatus."""
    tier = TicketTier.objects.create(
        event=event,
        name="Seed Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    return Ticket.objects.create(
        event=event,
        user=user,
        tier=tier,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=user.username,
    )


# ---------------------------------------------------------------------------
# get_user_event_status tests
# ---------------------------------------------------------------------------


class TestUserEventStatusCanPurchase:
    """Tests for can_purchase flag in get_user_event_status remaining_tickets."""

    def test_eligible_tier_has_can_purchase_true(
        self,
        event: Event,
        invited_user: RevelUser,
    ) -> None:
        """A tier the user can purchase has can_purchase=True."""
        _give_user_a_ticket(event, invited_user)
        tier = TicketTier.objects.create(
            event=event,
            name="Public",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        EventInvitation.objects.create(event=event, user=invited_user)

        status = get_user_event_status(event, invited_user)
        assert isinstance(status, UserEventStatus)
        remaining = {r.tier_id: r for r in status.remaining_tickets}
        assert remaining[tier.id].can_purchase is True

    def test_visible_but_not_eligible_tier_has_can_purchase_false(
        self,
        event: Event,
        invited_user: RevelUser,
    ) -> None:
        """A tier the user can see but can't purchase has can_purchase=False."""
        _give_user_a_ticket(event, invited_user)
        tier = TicketTier.objects.create(
            event=event,
            name="Restricted",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        # User has invitation but NOT linked to this tier
        EventInvitation.objects.create(event=event, user=invited_user)

        status = get_user_event_status(event, invited_user)
        assert isinstance(status, UserEventStatus)
        remaining = {r.tier_id: r for r in status.remaining_tickets}
        assert remaining[tier.id].can_purchase is False
        assert remaining[tier.id].remaining is None

    def test_mixed_tiers_both_flags(
        self,
        event: Event,
        invited_user: RevelUser,
    ) -> None:
        """Event with both purchasable and non-purchasable visible tiers."""
        _give_user_a_ticket(event, invited_user)
        public_tier = TicketTier.objects.create(
            event=event,
            name="Public",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        invited_tier = TicketTier.objects.create(
            event=event,
            name="Invited Only",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        EventInvitation.objects.create(event=event, user=invited_user)

        status = get_user_event_status(event, invited_user)
        assert isinstance(status, UserEventStatus)
        remaining = {r.tier_id: r for r in status.remaining_tickets}
        assert remaining[public_tier.id].can_purchase is True
        assert remaining[invited_tier.id].can_purchase is False
        # can_purchase_more should be True because public_tier is purchasable
        assert status.can_purchase_more is True

    def test_can_purchase_more_false_when_no_eligible_tiers(
        self,
        event: Event,
        invited_user: RevelUser,
    ) -> None:
        """can_purchase_more is False when all visible tiers are non-purchasable."""
        _give_user_a_ticket(event, invited_user)
        TicketTier.objects.create(
            event=event,
            name="Members Only",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        EventInvitation.objects.create(event=event, user=invited_user)

        status = get_user_event_status(event, invited_user)
        assert isinstance(status, UserEventStatus)
        assert status.can_purchase_more is False


# ---------------------------------------------------------------------------
# Staff/owner exemption in _assert_purchasable_by
# ---------------------------------------------------------------------------


class TestStaffOwnerPurchaseExemption:
    """Tests that staff and owners bypass purchasable_by restrictions."""

    def test_owner_can_purchase_invited_only_tier(
        self,
        event: Event,
        owner: RevelUser,
    ) -> None:
        """Org owner can purchase from INVITED tier without invitation."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService

        tier = TicketTier.objects.create(
            event=event,
            name="Invited",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        service = BatchTicketService(event, tier, owner)
        result = service.create_batch([TicketPurchaseItem(guest_name="Owner Test")])
        assert isinstance(result, list)
        assert len(result) == 1

    def test_staff_can_purchase_invited_only_tier(
        self,
        event: Event,
        staff_user: RevelUser,
    ) -> None:
        """Org staff can purchase from INVITED tier without invitation."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService

        tier = TicketTier.objects.create(
            event=event,
            name="Invited",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
            restrict_purchase_to_linked_invitations=True,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        service = BatchTicketService(event, tier, staff_user)
        result = service.create_batch([TicketPurchaseItem(guest_name="Staff Test")])
        assert isinstance(result, list)
        assert len(result) == 1

    def test_staff_can_purchase_members_only_tier(
        self,
        event: Event,
        staff_user: RevelUser,
    ) -> None:
        """Org staff can purchase from MEMBERS tier without membership."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService

        tier = TicketTier.objects.create(
            event=event,
            name="Members",
            visibility=VisibilityMixin.Visibility.PUBLIC,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        service = BatchTicketService(event, tier, staff_user)
        result = service.create_batch([TicketPurchaseItem(guest_name="Staff Test")])
        assert isinstance(result, list)
        assert len(result) == 1
