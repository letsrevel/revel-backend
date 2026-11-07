"""Door-scan ``membership`` block (#1320): read-only standing of the scanned person."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.schema import CheckInResponseSchema, MemberScanResponseSchema
from events.service import check_in_service, member_scan_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def open_door(day_event: Event) -> Event:
    """The event is under way, so check-in is open."""
    Event.objects.filter(pk=day_event.pk).update(
        start=timezone.now() - timedelta(minutes=5), end=timezone.now() + timedelta(hours=5)
    )
    day_event.refresh_from_db()
    return day_event


def test_check_in_of_day_member_reports_temporary_block(
    open_door: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    ticket = make_ticket(day_tier)
    checked_in = check_in_service.check_in_ticket(open_door, ticket.id, organization_owner_user)
    response = CheckInResponseSchema.from_orm(checked_in)
    assert response.membership is not None
    assert response.membership.kind == MembershipRecord.Kind.TEMPORARY
    assert response.membership.tier_name == day_tier.grants_membership_tier.name  # type: ignore[union-attr]
    assert response.membership.valid_until == open_door.end


def test_check_in_of_member_reports_standard_block(
    organization: Organization,
    open_door: Event,
    day_tier_factory: t.Callable[..., TicketTier],
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    tier = MembershipTier.objects.filter(organization=organization).first()
    assert tier is not None
    OrganizationMember.objects.create(organization=organization, user=holder, tier=tier)
    ticket = make_ticket(day_tier_factory(grants=False))
    checked_in = check_in_service.check_in_ticket(open_door, ticket.id, organization_owner_user)
    response = CheckInResponseSchema.from_orm(checked_in)
    assert response.membership is not None
    assert response.membership.kind == MembershipRecord.Kind.STANDARD
    assert response.membership.tier_name == tier.name
    assert response.membership.valid_until is None


def test_check_in_of_non_member_has_no_block(
    open_door: Event,
    day_tier_factory: t.Callable[..., TicketTier],
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    ticket = make_ticket(day_tier_factory(grants=False))
    checked_in = check_in_service.check_in_ticket(open_door, ticket.id, organization_owner_user)
    assert CheckInResponseSchema.from_orm(checked_in).membership is None


def test_member_card_scan_without_ticket_reports_membership(
    organization: Organization,
    open_door: Event,
    holder: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    tier = MembershipTier.objects.filter(organization=organization).first()
    assert tier is not None
    member = OrganizationMember.objects.create(organization=organization, user=holder, tier=tier)
    result = member_scan_service.scan_member_code(
        open_door, f"{OrganizationMember.QR_PREFIX}{member.pk}", organization_owner_user
    )
    assert result.checked_in is None
    response = MemberScanResponseSchema.from_result(result)
    assert response.membership is not None
    assert response.membership.kind == MembershipRecord.Kind.STANDARD
    assert response.membership.tier_name == tier.name
