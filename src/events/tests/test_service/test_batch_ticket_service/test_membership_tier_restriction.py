"""Tests for BatchTicketService._assert_membership_tier_allowed via create_batch.

`restricted_to_membership_tiers` was enforced by `ticket_service.get_eligible_tiers`
(which decides what the buyer is *offered*) but not by the batch purchase path, so a
direct checkout call could take a gated ticket (#807). These tests pin the guard on
the purchase path and assert the previously-unemitted `membership_tier_required` /
`upgrade_membership` identifiers actually reach the eligibility payload.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    MembershipTier,
    Organization,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.event_manager import NextStep, ReasonCode, UserIsIneligibleError

pytestmark = pytest.mark.django_db

PB = TicketTier.PurchasableBy


@pytest.fixture
def event(organization: Organization) -> Event:
    """An open, public, future-dated event."""
    return Event.objects.create(
        organization=organization,
        name="Gated Tier Event",
        slug="gated-tier-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        max_tickets_per_user=10,
    )


@pytest.fixture
def gold(organization: Organization) -> MembershipTier:
    """The membership tier the gated ticket tier requires."""
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def silver(organization: Organization) -> MembershipTier:
    """A membership tier the gated ticket tier does NOT accept."""
    return MembershipTier.objects.create(organization=organization, name="Silver")


@pytest.fixture
def gated_tier(event: Event, gold: MembershipTier) -> TicketTier:
    """A free MEMBERS tier restricted to the Gold membership tier."""
    tier = TicketTier.objects.create(
        event=event,
        name="Gold Only",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=PB.MEMBERS,
    )
    tier.restricted_to_membership_tiers.add(gold)
    return tier


@pytest.fixture
def open_gated_tier(event: Event, gold: MembershipTier) -> TicketTier:
    """A gated tier that invited non-members can also reach (INVITED_AND_MEMBERS)."""
    tier = TicketTier.objects.create(
        event=event,
        name="Gold Or Invited",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=PB.INVITED_AND_MEMBERS,
    )
    tier.restricted_to_membership_tiers.add(gold)
    return tier


def _buy(event: Event, tier: TicketTier, user: RevelUser) -> list[Ticket]:
    """Run a one-ticket batch purchase and return the created tickets."""
    result = BatchTicketService(event, tier, user).create_batch([TicketPurchaseItem(guest_name="Guest")])
    assert isinstance(result, list)
    return result


def test_invited_non_member_blocked_from_gated_tier(
    event: Event,
    open_gated_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """An invited non-member clears purchasable_by but not the membership-tier gate.

    This is the reachable hole #807 describes: `purchasable_by=INVITED_AND_MEMBERS`
    lets the invitation through, and nothing downstream looked at the restriction.
    """
    EventInvitation.objects.create(event=event, user=public_user)

    with pytest.raises(UserIsIneligibleError) as exc_info:
        _buy(event, open_gated_tier, public_user)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason_code == ReasonCode.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP
    assert Ticket.objects.filter(tier=open_gated_tier).count() == 0


def test_member_on_wrong_membership_tier_blocked(
    event: Event,
    gated_tier: TicketTier,
    organization: Organization,
    member_user: RevelUser,
    silver: MembershipTier,
) -> None:
    """An active member on a non-accepted membership tier is refused, with the upgrade hint."""
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    with pytest.raises(UserIsIneligibleError) as exc_info:
        _buy(event, gated_tier, member_user)

    eligibility = exc_info.value.eligibility
    assert eligibility.reason_code == ReasonCode.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP
    assert Ticket.objects.filter(tier=gated_tier).count() == 0


def test_member_without_any_membership_tier_blocked(
    event: Event,
    gated_tier: TicketTier,
    organization: Organization,
    member_user: RevelUser,
) -> None:
    """A tier-less member does not qualify — mirrors get_eligible_tiers."""
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=None,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    with pytest.raises(UserIsIneligibleError) as exc_info:
        _buy(event, gated_tier, member_user)

    assert exc_info.value.eligibility.reason_code == ReasonCode.MEMBERSHIP_TIER_REQUIRED


def test_member_on_allowed_membership_tier_can_purchase(
    event: Event,
    gated_tier: TicketTier,
    organization: Organization,
    member_user: RevelUser,
    gold: MembershipTier,
) -> None:
    """An active member on the required membership tier buys normally."""
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=gold,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    tickets = _buy(event, gated_tier, member_user)

    assert len(tickets) == 1
    assert tickets[0].status == Ticket.TicketStatus.ACTIVE


def test_member_on_one_of_several_allowed_tiers_can_purchase(
    event: Event,
    gated_tier: TicketTier,
    organization: Organization,
    member_user: RevelUser,
    silver: MembershipTier,
) -> None:
    """Holding any one of the restriction's membership tiers is enough."""
    gated_tier.restricted_to_membership_tiers.add(silver)
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    assert len(_buy(event, gated_tier, member_user)) == 1


def test_paused_member_on_allowed_tier_blocked(
    event: Event,
    open_gated_tier: TicketTier,
    organization: Organization,
    member_user: RevelUser,
    gold: MembershipTier,
) -> None:
    """Only ACTIVE memberships count, even on the required membership tier.

    The invitation carries the buyer past purchasable_by, so the paused Gold
    membership is judged by the membership-tier gate itself.
    """
    EventInvitation.objects.create(event=event, user=member_user)
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=gold,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    with pytest.raises(UserIsIneligibleError) as exc_info:
        _buy(event, open_gated_tier, member_user)

    assert exc_info.value.eligibility.reason_code == ReasonCode.MEMBERSHIP_TIER_REQUIRED
    assert Ticket.objects.filter(tier=open_gated_tier).count() == 0


def test_unrestricted_tier_is_unaffected(
    event: Event,
    organization: Organization,
    member_user: RevelUser,
    organization_membership: OrganizationMember,
) -> None:
    """A tier with no membership-tier restriction still sells to any member."""
    tier = TicketTier.objects.create(
        event=event,
        name="Members GA",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=PB.MEMBERS,
    )

    assert len(_buy(event, tier, member_user)) == 1


def test_org_owner_is_exempt(
    event: Event,
    gated_tier: TicketTier,
    organization_owner_user: RevelUser,
) -> None:
    """Org owners bypass the gate, consistent with _assert_purchasable_by."""
    assert len(_buy(event, gated_tier, organization_owner_user)) == 1
