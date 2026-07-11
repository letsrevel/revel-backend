"""Tests for organization admin membership request endpoints."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import MembershipTier, Organization, OrganizationMember, OrganizationMembershipRequest
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestManageMembershipRequests:
    def test_list_membership_requests_by_owner(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that an organization owner can list membership requests."""
        url = reverse("api:list_membership_requests", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200

    def test_approve_membership_request_by_owner(
        self, organization_owner_client: Client, organization_membership_request: OrganizationMembershipRequest
    ) -> None:
        """Test that an organization owner can approve a membership request."""
        # Get the default tier
        tier = MembershipTier.objects.get(
            organization=organization_membership_request.organization, name="General membership"
        )

        url = reverse(
            "api:approve_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        payload = {"tier_id": str(tier.id)}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 204
        organization_membership_request.refresh_from_db()
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.APPROVED

        # Verify member was created with correct tier
        member = OrganizationMember.objects.get(
            organization=organization_membership_request.organization, user=organization_membership_request.user
        )
        assert member.tier == tier
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE

    def test_reject_membership_request_by_owner(
        self, organization_owner_client: Client, organization_membership_request: OrganizationMembershipRequest
    ) -> None:
        """Test that an organization owner can reject a membership request."""
        url = reverse(
            "api:reject_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        response = organization_owner_client.post(url)
        assert response.status_code == 204
        organization_membership_request.refresh_from_db()
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.REJECTED

    def test_approve_dispatches_approval_notification(
        self,
        organization_owner_client: Client,
        organization_membership_request: OrganizationMembershipRequest,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Approving a request must persist the applicant's approval notification.

        Regression for #673: MEMBERSHIP_REQUEST_APPROVED was validated against a schema
        requiring role/action keys the dispatch never sends, so the notification failed
        validation and was silently swallowed.
        """
        tier = MembershipTier.objects.get(
            organization=organization_membership_request.organization, name="General membership"
        )
        url = reverse(
            "api:approve_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        payload = {"tier_id": str(tier.id)}
        with (
            patch("notifications.tasks.dispatch_notification.delay"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 204

        notifications = Notification.objects.filter(
            user=organization_membership_request.user,
            notification_type=NotificationType.MEMBERSHIP_REQUEST_APPROVED,
        )
        assert notifications.count() == 1, "approval notification failed schema validation and was swallowed"

    def test_reject_dispatches_rejection_notification(
        self,
        organization_owner_client: Client,
        organization_membership_request: OrganizationMembershipRequest,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Rejecting a request must persist the applicant's rejection notification (regression for #673)."""
        url = reverse(
            "api:reject_membership_request",
            kwargs={
                "slug": organization_membership_request.organization.slug,
                "request_id": organization_membership_request.id,
            },
        )
        with (
            patch("notifications.tasks.dispatch_notification.delay"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = organization_owner_client.post(url)
        assert response.status_code == 204

        notifications = Notification.objects.filter(
            user=organization_membership_request.user,
            notification_type=NotificationType.MEMBERSHIP_REQUEST_REJECTED,
        )
        assert notifications.count() == 1, "rejection notification failed schema validation and was swallowed"
