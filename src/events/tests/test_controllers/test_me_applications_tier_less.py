"""Controller tests for tier-less (legacy) membership applications via POST /apply.

Split from test_me_applications.py to respect the 1000-line file limit.
"""

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


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


def test_tier_less_apply_from_active_member_conflicts(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """An active member applying with tier_id=null must 409, not mint a stray PENDING row.

    Tier-less rows never auto-complete, so the row would wait in the staff queue
    and, on approval, silently overwrite the member's tier with whatever staff
    pick — with nothing signalling the "applicant" was already active.
    """
    OrganizationMember.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": None}, content_type="application/json")

    assert response.status_code == 409, response.content
    assert not OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()


def test_tier_less_apply_from_non_member_still_creates_pending_row(
    nonmember_user: RevelUser, organization: Organization
) -> None:
    """The legacy path stays open: a non-member's tier-less application is created and stays PENDING."""
    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": None}, content_type="application/json")

    assert response.status_code == 201, response.content
    application = OrganizationMembershipRequest.objects.get(organization=organization, user=nonmember_user)
    assert application.tier_id is None
    assert application.status == OrganizationMembershipRequest.Status.PENDING
