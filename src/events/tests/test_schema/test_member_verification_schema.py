"""Member verification schema tests."""

import typing as t

import pytest

from events import schema
from events.models import OrganizationMember


@pytest.fixture
def member(organization_membership: OrganizationMember) -> OrganizationMember:
    """Provide an OrganizationMember with user and tier select_related."""
    organization_membership.refresh_from_db()
    return organization_membership


@pytest.mark.django_db
def test_from_member_includes_status_tier_and_user(member: OrganizationMember) -> None:
    result = schema.MemberVerificationSchema.from_member(member)
    assert result.member_id == member.id
    assert result.status == member.status
    assert t.cast(t.Any, result.user).id == member.user_id
    assert result.member_since == member.created_at


def test_check_in_response_kind_default() -> None:
    assert schema.CheckInResponseSchema.model_fields["kind"].default == "checked_in"
