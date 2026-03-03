"""Tests for CanPurchaseTicket permission class.

Verifies that only ACTIVE organization members can purchase member-only ticket tiers.
Users with BANNED, CANCELLED, or PAUSED membership status must be denied.
"""

import typing as t
from datetime import timedelta

import pytest
from django.http import HttpRequest
from django.utils import timezone
from ninja_extra.exceptions import PermissionDenied

from accounts.models import RevelUser
from events.controllers.permissions import CanPurchaseTicket
from events.models import (
    Event,
    Organization,
    OrganizationMember,
    TicketTier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def members_tier(event: Event) -> TicketTier:
    """A ticket tier purchasable by members only."""
    return TicketTier.objects.create(
        event=event,
        name="Members Only Tier",
        price=10.00,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
    )


@pytest.fixture
def invited_and_members_tier(event: Event) -> TicketTier:
    """A ticket tier purchasable by invited and members."""
    return TicketTier.objects.create(
        event=event,
        name="Invited and Members Tier",
        price=15.00,
        purchasable_by=TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
    )


def _make_request(user: RevelUser) -> HttpRequest:
    """Create a mock HttpRequest with the given user."""
    request = HttpRequest()
    request.user = user
    return request


class TestCanPurchaseTicketMembershipStatus:
    """Tests ensuring CanPurchaseTicket respects membership status."""

    def test_active_member_can_purchase_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        members_tier: TicketTier,
    ) -> None:
        """An ACTIVE member should be allowed to purchase a members-only tier."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        result = permission.has_object_permission(request, t.cast(t.Any, None), members_tier)

        assert result is True

    def test_banned_member_cannot_purchase_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        members_tier: TicketTier,
    ) -> None:
        """A BANNED member must NOT be allowed to purchase a members-only tier."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        with pytest.raises(PermissionDenied):
            permission.has_object_permission(request, t.cast(t.Any, None), members_tier)

    def test_cancelled_member_cannot_purchase_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        members_tier: TicketTier,
    ) -> None:
        """A CANCELLED member must NOT be allowed to purchase a members-only tier."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        with pytest.raises(PermissionDenied):
            permission.has_object_permission(request, t.cast(t.Any, None), members_tier)

    def test_paused_member_cannot_purchase_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        members_tier: TicketTier,
    ) -> None:
        """A PAUSED member must NOT be allowed to purchase a members-only tier."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        with pytest.raises(PermissionDenied):
            permission.has_object_permission(request, t.cast(t.Any, None), members_tier)

    def test_active_member_can_purchase_invited_and_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        invited_and_members_tier: TicketTier,
    ) -> None:
        """An ACTIVE member should be allowed to purchase an invited-and-members tier."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        result = permission.has_object_permission(request, t.cast(t.Any, None), invited_and_members_tier)

        assert result is True

    def test_banned_member_cannot_purchase_invited_and_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        invited_and_members_tier: TicketTier,
    ) -> None:
        """A BANNED member must NOT be allowed to purchase an invited-and-members tier (via membership)."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.BANNED,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        with pytest.raises(PermissionDenied):
            permission.has_object_permission(request, t.cast(t.Any, None), invited_and_members_tier)

    def test_cancelled_member_cannot_purchase_invited_and_members_tier(
        self,
        member_user: RevelUser,
        organization: Organization,
        invited_and_members_tier: TicketTier,
    ) -> None:
        """A CANCELLED member must NOT be allowed to purchase an invited-and-members tier (via membership)."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        with pytest.raises(PermissionDenied):
            permission.has_object_permission(request, t.cast(t.Any, None), invited_and_members_tier)

    def test_nonmember_cannot_purchase_members_tier(
        self,
        nonmember_user: RevelUser,
        members_tier: TicketTier,
    ) -> None:
        """A user with no membership at all must NOT be allowed to purchase a members-only tier."""
        permission = CanPurchaseTicket()
        request = _make_request(nonmember_user)

        with pytest.raises(PermissionDenied):
            permission.has_object_permission(request, t.cast(t.Any, None), members_tier)

    def test_org_owner_can_always_purchase_members_tier(
        self,
        organization_owner_user: RevelUser,
        members_tier: TicketTier,
    ) -> None:
        """The organization owner should always be able to purchase, regardless of membership."""
        permission = CanPurchaseTicket()
        request = _make_request(organization_owner_user)

        result = permission.has_object_permission(request, t.cast(t.Any, None), members_tier)

        assert result is True

    def test_outside_sales_window_denied(
        self,
        member_user: RevelUser,
        organization: Organization,
        event: Event,
    ) -> None:
        """A tier outside its sales window should deny purchase even for active members."""
        OrganizationMember.objects.create(
            organization=organization,
            user=member_user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        # Create a tier whose sales window has already ended
        past_tier = TicketTier.objects.create(
            event=event,
            name="Expired Tier",
            price=10.00,
            purchasable_by=TicketTier.PurchasableBy.MEMBERS,
            sales_start_at=timezone.now() - timedelta(days=7),
            sales_end_at=timezone.now() - timedelta(days=1),
        )
        permission = CanPurchaseTicket()
        request = _make_request(member_user)

        with pytest.raises(PermissionDenied, match="outside of the sale window"):
            permission.has_object_permission(request, t.cast(t.Any, None), past_tier)
