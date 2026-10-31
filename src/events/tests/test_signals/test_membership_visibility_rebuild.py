"""Membership status changes rebuild the persisted attendee visibility flags of the org's events."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationMember

pytestmark = pytest.mark.django_db

TASK = "events.signals.build_attendee_visibility_flags"


@patch(TASK)
def test_status_change_rebuilds_org_events(
    mock_task: MagicMock,
    organization: Organization,
    public_event: Event,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user)
    mock_task.delay.reset_mock()
    with django_capture_on_commit_callbacks(execute=True):
        member.status = OrganizationMember.MembershipStatus.BANNED
        member.save()
    mock_task.delay.assert_any_call(str(public_event.id))


@patch(TASK)
def test_unrelated_save_does_not_rebuild(
    mock_task: MagicMock,
    organization: Organization,
    public_event: Event,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user)
    with django_capture_on_commit_callbacks(execute=True):
        member.save()
    mock_task.delay.assert_not_called()


@patch(TASK)
def test_membership_removal_rebuilds(
    mock_task: MagicMock,
    organization: Organization,
    public_event: Event,
    member_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user)
    with django_capture_on_commit_callbacks(execute=True):
        member.delete()
    mock_task.delay.assert_any_call(str(public_event.id))
