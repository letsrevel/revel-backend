"""Tests for organization admin membership request endpoints."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
from notifications.enums import NotificationType
from notifications.models import Notification
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission

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
        assert organization_membership_request.status == OrganizationMembershipRequest.Status.COMPLETED

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


class TestMembershipRequestSerialization:
    """The admin Requests tab needs each row's target tier and questionnaire submission (#790)."""

    @staticmethod
    def _list_rows(client: Client, organization: Organization) -> list[dict[str, t.Any]]:
        url = reverse("api:list_membership_requests", kwargs={"slug": organization.slug})
        response = client.get(url)
        assert response.status_code == 200
        results: list[dict[str, t.Any]] = response.json()["results"]
        return results

    def test_row_carries_tier_and_submission_with_evaluation(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        org_questionnaire: OrganizationQuestionnaire,
        submitted_submission: QuestionnaireSubmission,
        approved_evaluation: QuestionnaireEvaluation,
    ) -> None:
        """A tier-bound application with a submission exposes the tier and the OQ-keyed submission info."""
        tier = MembershipTier.objects.create(organization=organization, name="Standard")
        application = OrganizationMembershipRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            tier=tier,
            questionnaire_submission=submitted_submission,
        )

        rows = self._list_rows(organization_owner_client, organization)
        row = next(r for r in rows if r["id"] == str(application.id))

        assert row["tier"]["id"] == str(tier.id)
        assert row["tier"]["name"] == "Standard"
        assert "requires_membership_approval" not in row["tier"], "admin policy fields must not leak here"

        submission_info = row["questionnaire_submission"]
        assert submission_info["id"] == str(submitted_submission.id)
        # The FE review route is keyed on the OrganizationQuestionnaire, not the raw questionnaire.
        assert submission_info["org_questionnaire_id"] == str(org_questionnaire.id)
        assert submission_info["org_questionnaire_id"] != str(submitted_submission.questionnaire_id)
        assert submission_info["evaluation_status"] == approved_evaluation.status

    def test_row_with_submission_but_no_evaluation(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        org_questionnaire: OrganizationQuestionnaire,
        submitted_submission: QuestionnaireSubmission,
    ) -> None:
        """An unevaluated submission still resolves, with a null evaluation_status."""
        application = OrganizationMembershipRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            questionnaire_submission=submitted_submission,
        )

        rows = self._list_rows(organization_owner_client, organization)
        row = next(r for r in rows if r["id"] == str(application.id))

        submission_info = row["questionnaire_submission"]
        assert submission_info["id"] == str(submitted_submission.id)
        assert submission_info["org_questionnaire_id"] == str(org_questionnaire.id)
        assert submission_info["evaluation_status"] is None

    def test_row_hides_submission_when_org_questionnaire_is_gone(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        org_questionnaire: OrganizationQuestionnaire,
        submitted_submission: QuestionnaireSubmission,
    ) -> None:
        """A submission whose OrganizationQuestionnaire was deleted is omitted, not half-serialized."""
        application = OrganizationMembershipRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            questionnaire_submission=submitted_submission,
        )
        org_questionnaire.delete()

        rows = self._list_rows(organization_owner_client, organization)
        row = next(r for r in rows if r["id"] == str(application.id))

        assert row["questionnaire_submission"] is None

    def test_legacy_tier_less_row_serializes_cleanly(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_membership_request: OrganizationMembershipRequest,
    ) -> None:
        """Legacy rows carry neither tier nor submission and must still serialize (regression)."""
        rows = self._list_rows(organization_owner_client, organization)
        row = next(r for r in rows if r["id"] == str(organization_membership_request.id))

        assert row["tier"] is None
        assert row["questionnaire_submission"] is None
