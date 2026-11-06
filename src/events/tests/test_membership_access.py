"""Tests for the membership access predicates in ``events/utils/membership_access.py``."""

import typing as t

import pytest
from django.db.models import QuerySet

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMember
from events.utils.membership_access import (
    BANNED_STATUS,
    ELIGIBLE_STATUSES,
    VISIBLE_STATUSES,
    banned_org_ids,
    eligible_member_org_ids,
    eligible_member_user_ids,
    get_eligible_membership,
    is_eligible_member,
    is_eligible_status,
    is_visible_status,
    visible_member_org_ids,
    visible_member_user_ids,
)

pytestmark = pytest.mark.django_db

S = OrganizationMember.MembershipStatus


def test_status_constants_match_model_enum() -> None:
    assert VISIBLE_STATUSES == {S.ACTIVE, S.PAUSED}
    assert ELIGIBLE_STATUSES == {S.ACTIVE}
    assert BANNED_STATUS == S.BANNED
    # A new status must force a decision about the predicates here.
    assert set(S.values) == {"active", "paused", "cancelled", "banned"}


@pytest.mark.parametrize(
    "status,visible,eligible",
    [
        ("active", True, True),
        ("paused", True, False),
        ("cancelled", False, False),
        ("banned", False, False),
        (None, False, False),
    ],
)
def test_status_helpers(status: str | None, visible: bool, eligible: bool) -> None:
    assert is_visible_status(status) is visible
    assert is_eligible_status(status) is eligible


@pytest.fixture
def orgs_by_status(user: RevelUser, organization_owner_user: RevelUser) -> dict[str, Organization]:
    """One organization per membership status, with ``user`` as the member."""
    result: dict[str, Organization] = {}
    for status in S.values:
        org = Organization.objects.create(name=f"Org {status}", slug=f"org-{status}", owner=organization_owner_user)
        OrganizationMember.objects.create(organization=org, user=user, status=status)
        result[status] = org
    return result


def test_org_id_builders_by_status(user: RevelUser, orgs_by_status: dict[str, Organization]) -> None:
    ids = {k: o.id for k, o in orgs_by_status.items()}
    assert set(visible_member_org_ids(user)) == {ids["active"], ids["paused"]}
    assert set(eligible_member_org_ids(user)) == {ids["active"]}
    assert set(banned_org_ids(user)) == {ids["banned"]}


def test_user_id_builders_with_tier_filter(
    user: RevelUser, organization: Organization, django_user_model: t.Type[RevelUser]
) -> None:
    paused_user = django_user_model.objects.create_user(username="paused", email="paused@example.com", password="x")
    cancelled_user = django_user_model.objects.create_user(username="canc", email="canc@example.com", password="x")
    default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
    other_tier = MembershipTier.objects.create(organization=organization, name="Other")
    OrganizationMember.objects.create(organization=organization, user=user, status=S.ACTIVE, tier=default_tier)
    OrganizationMember.objects.create(organization=organization, user=paused_user, status=S.PAUSED, tier=other_tier)
    OrganizationMember.objects.create(organization=organization, user=cancelled_user, status=S.CANCELLED)

    assert set(visible_member_user_ids(organization.id)) == {user.id, paused_user.id}
    assert set(eligible_member_user_ids(organization.id)) == {user.id}
    assert set(eligible_member_user_ids(organization.id, tier_ids=[default_tier.id])) == {user.id}
    assert set(eligible_member_user_ids(organization.id, tier_ids=[other_tier.id])) == set()


def test_builders_are_lazy(user: RevelUser, organization: Organization) -> None:
    assert isinstance(visible_member_org_ids(user), QuerySet)
    assert isinstance(eligible_member_org_ids(user), QuerySet)
    assert isinstance(banned_org_ids(user), QuerySet)
    assert isinstance(visible_member_user_ids(organization.id), QuerySet)
    assert isinstance(eligible_member_user_ids(organization.id), QuerySet)


def test_get_eligible_membership_returns_active_with_tier(
    user: RevelUser, orgs_by_status: dict[str, Organization], django_assert_num_queries: t.Any
) -> None:
    active = orgs_by_status["active"]
    with django_assert_num_queries(1):
        membership = get_eligible_membership(user, active.id)
        assert membership is not None
        _ = membership.tier  # select_related: no extra query
    assert is_eligible_member(user, active.id)
    for status in ("paused", "cancelled", "banned"):
        assert get_eligible_membership(user, orgs_by_status[status].id) is None
        assert not is_eligible_member(user, orgs_by_status[status].id)
