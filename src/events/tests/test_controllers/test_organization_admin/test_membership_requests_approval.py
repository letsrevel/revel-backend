"""Regression tests for staff approve/reject after tier-aware extensions."""

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)

pytestmark = pytest.mark.django_db


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


def test_approve_legacy_tier_less_request_requires_tier_id(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    client = _client(organization_owner_user)
    url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": app.id})

    # Without tier_id → 400
    response = client.post(url, data={}, content_type="application/json")
    assert response.status_code == 400

    # With tier_id → 204
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
    assert response.status_code == 204
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED
    assert OrganizationMember.objects.filter(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).exists()


def test_approve_application_with_carried_tier_does_not_need_tier_id(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    client = _client(organization_owner_user)
    url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": app.id})
    response = client.post(url, data={}, content_type="application/json")
    assert response.status_code == 204
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED
    # No staff tier supplied → the applicant's own choice is what gets granted.
    assert app.tier == tier
    member = OrganizationMember.objects.get(organization=organization, user=nonmember_user)
    assert member.tier == tier


def test_staff_tier_id_overrides_the_applicants_tier(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    """An explicit ``tier_id`` must win over the tier the applicant selected.

    Regression: the precedence used to be ``application.tier or tier``, so staff
    correcting an applicant's tier were answered 204 while their choice was
    silently discarded.
    """
    other_tier = MembershipTier.objects.create(organization=organization, name="Premium")
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    client = _client(organization_owner_user)
    url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": app.id})
    response = client.post(url, data={"tier_id": str(other_tier.id)}, content_type="application/json")
    assert response.status_code == 204

    member = OrganizationMember.objects.get(organization=organization, user=nonmember_user)
    assert member.tier == other_tier, "staff tier override was discarded"
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED
    # The row records what was actually granted, not the superseded request.
    assert app.tier == other_tier


def test_reject_completed_application_is_refused(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    """A COMPLETED application must not regress to REJECTED.

    Regression: reject had no status guard, so staff could flip the history of an
    already-granted membership to REJECTED while the member stayed ACTIVE —
    ``/me/applications`` then reported a rejection to an active member.
    """
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.COMPLETED,
    )
    member = OrganizationMember.objects.create(organization=organization, user=nonmember_user, tier=tier)

    client = _client(organization_owner_user)
    url = reverse("api:reject_membership_request", kwargs={"slug": organization.slug, "request_id": app.id})
    response = client.post(url)

    assert response.status_code == 400
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.COMPLETED
    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.ACTIVE


def test_reject_pending_application_still_works(
    organization_owner_user: RevelUser,
    organization: Organization,
    nonmember_user: RevelUser,
    tier: MembershipTier,
) -> None:
    """The new guard must not break the normal staff rejection."""
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    client = _client(organization_owner_user)
    url = reverse("api:reject_membership_request", kwargs={"slug": organization.slug, "request_id": app.id})
    response = client.post(url)

    assert response.status_code == 204
    app.refresh_from_db()
    assert app.status == OrganizationMembershipRequest.Status.REJECTED
    assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
