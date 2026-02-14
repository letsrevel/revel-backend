"""Tests for eligibility checks and membership tier restrictions."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipTier,
    OrganizationMember,
    TicketTier,
)
from events.service.event_manager import EventManager, NextStep, Reasons, UserIsIneligibleError
from events.service.ticket_service import get_eligible_tiers

pytestmark = pytest.mark.django_db


# --- Test Cases for Eligibility Checks ---


def test_raises_error_for_ineligible_user(public_user: RevelUser, members_only_event: Event) -> None:
    """Verify that check_eligibility raises for ineligible user with raise_on_false=True."""
    handler = EventManager(user=public_user, event=members_only_event)

    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.check_eligibility(raise_on_false=True)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERS_ONLY
    assert eligibility.next_step == NextStep.BECOME_MEMBER


def test_bypass_eligibility_allows_ineligible_user(public_user: RevelUser, members_only_event: Event) -> None:
    """Verify that bypass=True makes an ineligible user pass eligibility check."""
    handler = EventManager(user=public_user, event=members_only_event)

    eligibility = handler.check_eligibility(bypass=True)

    assert eligibility.allowed is True


# --- Test Cases for Membership Tier Restrictions via get_eligible_tiers ---


def test_ticket_tier_without_membership_restriction_allows_all(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember
) -> None:
    """Ticket tiers without membership restrictions allow any member to see them."""
    tier = TicketTier.objects.create(
        event=public_event,
        name="Open Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
    )

    eligible = get_eligible_tiers(public_event, member_user)

    assert tier in eligible


def test_ticket_tier_with_membership_restriction_allows_correct_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """User with required membership tier can see restricted ticket tier."""
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    OrganizationMember.objects.create(
        organization=organization, user=member_user, tier=gold_tier, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    eligible = get_eligible_tiers(public_event, member_user)

    assert ticket_tier in eligible


def test_ticket_tier_with_membership_restriction_blocks_wrong_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """User with wrong membership tier cannot see restricted ticket tier."""
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    eligible = get_eligible_tiers(public_event, member_user)

    assert ticket_tier not in eligible


def test_ticket_tier_with_membership_restriction_blocks_non_member(
    public_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Non-member cannot see membership-restricted ticket tier."""
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    eligible = get_eligible_tiers(public_event, public_user)

    assert ticket_tier not in eligible


def test_ticket_tier_with_membership_restriction_allows_multiple_tiers(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Ticket tier restricted to multiple membership tiers allows any of them."""
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    platinum_tier = MembershipTier.objects.create(organization=organization, name="Platinum")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    ticket_tier = TicketTier.objects.create(
        event=public_event, name="Premium Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier, platinum_tier, silver_tier)

    eligible = get_eligible_tiers(public_event, member_user)

    assert ticket_tier in eligible


def test_ticket_tier_with_membership_restriction_blocks_inactive_member(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """User with paused membership cannot see tier-restricted ticket."""
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=gold_tier,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    eligible = get_eligible_tiers(public_event, member_user)

    assert ticket_tier not in eligible


def test_ticket_tier_with_membership_restriction_blocks_member_without_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Member without any tier cannot see tier-restricted ticket."""
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    OrganizationMember.objects.create(
        organization=organization, user=member_user, tier=None, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    eligible = get_eligible_tiers(public_event, member_user)

    assert ticket_tier not in eligible
