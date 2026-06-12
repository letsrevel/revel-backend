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
