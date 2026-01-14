"""Tests for ticket creation, waives purchase, and membership tier restrictions."""

import typing as t
from unittest.mock import patch

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    MembershipTier,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.service.event_manager import EventManager, NextStep, Reasons, UserIsIneligibleError

pytestmark = pytest.mark.django_db


# --- Test Cases for Ticket Creation ---


def test_creates_ticket_for_eligible_user(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """
    Verify that a ticket is successfully created for an eligible user.
    The correct tier ("member") should be determined and created.
    """
    handler = EventManager(user=member_user, event=public_event)

    assert Ticket.objects.count() == 0

    # Act
    ticket = handler.create_ticket(free_tier)
    assert isinstance(ticket, Ticket)

    # Assert
    assert Ticket.objects.count() == 1
    assert ticket.user == member_user
    assert ticket.event == public_event
    assert ticket.status == Ticket.TicketStatus.ACTIVE

    # Verify the "member" tier was created and assigned
    assert public_event.ticket_tiers.filter(name=free_tier.name).exists()


def test_create_ticket_is_idempotent(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """
    Verify that calling create_ticket multiple times does not create duplicate tickets
    due to the use of get_or_create.
    """
    handler = EventManager(user=member_user, event=public_event)

    # Act
    handler.create_ticket(free_tier)
    with pytest.raises(HttpError):
        handler.create_ticket(free_tier)

    # Assert
    assert Ticket.objects.count() == 1
    assert public_event.ticket_tiers.filter(name=free_tier.name).exists()


def test_raises_error_for_ineligible_user(
    public_user: RevelUser, members_only_event: Event, free_tier: TicketTier
) -> None:
    """
    Verify that UserIsIneligibleError is raised when attempting to create a ticket
    for a user who does not pass the eligibility checks.
    """
    handler = EventManager(user=public_user, event=members_only_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(free_tier)

    # Assert that the exception contains the correct, detailed eligibility object
    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERS_ONLY
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.BECOME_MEMBER
    assert Ticket.objects.count() == 0


def test_bypass_eligibility_creates_ticket_for_ineligible_user(
    public_user: RevelUser, members_only_event: Event, free_tier: TicketTier
) -> None:
    """
    Verify that setting bypass_eligibility_checks=True successfully creates a ticket
    for a user who would otherwise be ineligible.
    """
    handler = EventManager(user=public_user, event=members_only_event)

    # Act
    ticket = handler.create_ticket(free_tier, bypass_eligibility_checks=True)
    assert isinstance(ticket, Ticket)

    # Assert
    assert Ticket.objects.count() == 1
    assert ticket.user == public_user


# --- Test Cases for Waives Purchase Logic ---


def test_invitation_waives_purchase_creates_complimentary_ticket(
    public_user: RevelUser, public_event: Event, free_tier: TicketTier
) -> None:
    """Test that invitation with waives_purchase=True creates complimentary ACTIVE ticket."""
    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    ticket = handler.create_ticket(free_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.user == public_user
    assert ticket.event == public_event
    assert ticket.tier == free_tier


def test_waives_purchase_increments_quantity_sold(public_user: RevelUser, public_event: Event) -> None:
    """Test that complimentary tickets properly increment quantity_sold."""
    # Create tier with quantity tracking
    tier = TicketTier.objects.create(
        event=public_event,
        name="Limited Tier",
        total_quantity=10,
        quantity_sold=5,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    handler.create_ticket(tier)

    # Assert - quantity_sold should be incremented
    tier.refresh_from_db()
    assert tier.quantity_sold == 6


def test_waives_purchase_bypasses_payment_flow(public_user: RevelUser, public_event: Event) -> None:
    """Test that waives_purchase bypasses normal payment flow for paid tiers."""
    # Create paid tier
    paid_tier = TicketTier.objects.create(
        event=public_event,
        name="Paid Tier",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    ticket = handler.create_ticket(paid_tier)

    # Assert - should get direct ticket, not payment flow
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert not hasattr(ticket, "payment")  # No payment object should be created


def test_normal_user_without_waives_purchase_gets_payment_flow(public_user: RevelUser, public_event: Event) -> None:
    """Test that normal users without waives_purchase go through payment flow."""
    # Create paid tier
    paid_tier = TicketTier.objects.create(
        event=public_event,
        name="Paid Tier",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    handler = EventManager(user=public_user, event=public_event)

    # Mock the ticket service checkout to return a checkout URL
    with patch("events.service.ticket_service.TicketService.checkout") as mock_checkout:
        mock_checkout.return_value = "https://checkout.stripe.com/mock-url"

        # Act
        result = handler.create_ticket(paid_tier)

        # Assert - should get checkout URL string, not ticket object
        assert isinstance(result, str)
        assert result.startswith("https://")  # Should be Stripe checkout URL
        mock_checkout.assert_called_once()


def test_waives_purchase_respects_capacity_limits(
    public_user: RevelUser, member_user: RevelUser, public_event: Event
) -> None:
    """Test that complimentary tickets still respect tier capacity limits."""
    # Create tier at capacity
    tier = TicketTier.objects.create(
        event=public_event,
        name="Limited Tier",
        total_quantity=1,
        quantity_sold=1,  # Already at capacity
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act & Assert - should still fail due to capacity
    with pytest.raises(UserIsIneligibleError) as exc_info:  # Should raise some capacity-related error
        handler.create_ticket(tier)
    assert exc_info.value.eligibility.reason == Reasons.SOLD_OUT


def test_waives_purchase_works_with_free_tiers(
    public_user: RevelUser, public_event: Event, free_tier: TicketTier
) -> None:
    """Test that waives_purchase works correctly with already-free tiers."""
    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    ticket = handler.create_ticket(free_tier)

    # Assert - should still create complimentary ticket (bypassing any free flow)
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


# --- Test Cases for Membership Tier Restrictions ---


def test_ticket_tier_without_membership_restriction_allows_all(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """Test that ticket tiers without membership restrictions allow any member to purchase."""
    # free_tier has no restricted_to_membership_tiers set
    handler = EventManager(user=member_user, event=public_event)

    # Act
    ticket = handler.create_ticket(free_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_ticket_tier_with_membership_restriction_allows_correct_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that user with required membership tier can purchase restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Assign user to gold tier
    OrganizationMember.objects.create(
        organization=organization, user=member_user, tier=gold_tier, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act
    ticket = handler.create_ticket(ticket_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_ticket_tier_with_membership_restriction_blocks_wrong_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that user with wrong membership tier cannot purchase restricted ticket."""
    # Create membership tiers
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    # Assign user to silver tier
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    # Create ticket tier restricted to gold members only
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP


def test_ticket_tier_with_membership_restriction_blocks_non_member(
    public_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that non-member cannot purchase membership-restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=public_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP


def test_ticket_tier_with_membership_restriction_allows_multiple_tiers(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that ticket tier restricted to multiple membership tiers allows any of them."""
    # Create membership tiers
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    platinum_tier = MembershipTier.objects.create(organization=organization, name="Platinum")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    # Assign user to silver tier
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    # Create ticket tier restricted to silver OR gold OR platinum
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="Premium Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier, platinum_tier, silver_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act
    ticket = handler.create_ticket(ticket_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_ticket_tier_with_membership_restriction_blocks_inactive_member(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that user with paused/cancelled membership cannot purchase restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Assign user to gold tier but with PAUSED status
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=gold_tier,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP


def test_ticket_tier_with_membership_restriction_waived_by_invitation(
    public_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that invitation with waives_membership_required bypasses membership tier requirement."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    # Create invitation that waives membership requirement
    EventInvitation.objects.create(user=public_user, event=public_event, waives_membership_required=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act - should succeed despite not having required tier
    result = handler.create_ticket(ticket_tier)

    # Assert - will go through payment flow since waives_purchase is False
    # But membership tier check should be bypassed
    assert result is not None


def test_ticket_tier_with_membership_restriction_blocks_member_without_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that member without any tier cannot purchase tier-restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create member without tier assignment
    OrganizationMember.objects.create(
        organization=organization, user=member_user, tier=None, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP
