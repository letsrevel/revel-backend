"""Membership writes rebuild the persisted attendee visibility flags of the member's own events."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationMember, Ticket, TicketTier

pytestmark = pytest.mark.django_db

TASK = "events.service.user_preferences_service.build_attendee_visibility_flags"


@patch(TASK)
def test_status_change_rebuilds_members_events(
    mock_task: MagicMock,
    organization: Organization,
    public_event: Event,
    vip_tier: TicketTier,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    Ticket.objects.create(event=public_event, user=member_user, tier=vip_tier, guest_name="Guest")
    member = OrganizationMember.objects.create(organization=organization, user=member_user)
    mock_task.delay.reset_mock()
    with django_capture_on_commit_callbacks(execute=True):
        member.status = OrganizationMember.MembershipStatus.BANNED
        member.save()
    mock_task.delay.assert_any_call(str(public_event.id))


@patch(TASK)
def test_membership_removal_rebuilds_members_events(
    mock_task: MagicMock,
    organization: Organization,
    public_event: Event,
    vip_tier: TicketTier,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    Ticket.objects.create(event=public_event, user=member_user, tier=vip_tier, guest_name="Guest")
    member = OrganizationMember.objects.create(organization=organization, user=member_user)
    mock_task.delay.reset_mock()
    with django_capture_on_commit_callbacks(execute=True):
        member.delete()
    mock_task.delay.assert_any_call(str(public_event.id))


@patch(TASK)
def test_member_without_participation_rebuilds_nothing(
    mock_task: MagicMock,
    organization: Organization,
    public_event: Event,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        OrganizationMember.objects.create(organization=organization, user=member_user)
    mock_task.delay.assert_not_called()
