"""Ensure the legacy /membership-requests endpoint still works after model expansion."""

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization, OrganizationMembershipRequest

pytestmark = pytest.mark.django_db


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


def test_legacy_membership_request_creates_tier_less_pending(
    nonmember_user: RevelUser, organization: Organization
) -> None:
    organization.accept_membership_requests = True
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["accept_membership_requests", "visibility"])

    client = _client(nonmember_user)
    url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
    response = client.post(url, data={"message": "hello"}, content_type="application/json")
    assert response.status_code == 200

    app = OrganizationMembershipRequest.objects.get(organization=organization, user=nonmember_user)
    assert app.tier_id is None
    assert app.plan_id is None
    assert app.status == OrganizationMembershipRequest.Status.PENDING
    assert app.message == "hello"
