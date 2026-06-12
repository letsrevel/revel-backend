"""Verify membership-application notifications still fire end-to-end."""

from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
)
from events.service import organization_service
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db(transaction=True)


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


def test_apply_fires_request_created_notification_for_staff(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.default_requires_membership_approval = True  # force PENDING (so we keep the row)
    organization.save(
        update_fields=[
            "visibility",
            "accept_membership_requests",
            "default_requires_membership_approval",
        ]
    )

    with patch("notifications.signals.membership.notification_requested.send") as mock_send:
        client = _client(nonmember_user)
        url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
        response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")
        assert response.status_code == 201

    # The first POST creates a PENDING OMR → post_save signal fires.
    types_sent = [call.kwargs.get("notification_type") for call in mock_send.call_args_list]
    assert NotificationType.MEMBERSHIP_REQUEST_CREATED in types_sent


def test_approve_fires_approved_notification(
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
    with patch("events.service.organization_service.notification_requested.send") as mock_send:
        organization_service.approve_membership_request(app, organization_owner_user)
    types_sent = [call.kwargs.get("notification_type") for call in mock_send.call_args_list]
    assert NotificationType.MEMBERSHIP_REQUEST_APPROVED in types_sent


def test_reject_fires_rejected_notification(
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
    with patch("events.service.organization_service.notification_requested.send") as mock_send:
        organization_service.reject_membership_request(app, organization_owner_user)
    types_sent = [call.kwargs.get("notification_type") for call in mock_send.call_args_list]
    assert NotificationType.MEMBERSHIP_REQUEST_REJECTED in types_sent
