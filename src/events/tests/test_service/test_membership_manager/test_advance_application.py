"""Tests for advance_application state-advance-on-read helper."""

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
from events.service.membership_manager import advance_application
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Standard")


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def test_pending_free_with_no_gates_completes_and_creates_member(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    result, eligibility = advance_application(app)
    assert result.status == OrganizationMembershipRequest.Status.COMPLETED
    assert eligibility.allowed is True
    assert OrganizationMember.objects.filter(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).exists()


def test_pending_with_pending_questionnaire_stays_pending(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    result, _eligibility = advance_application(app)
    assert result.status == OrganizationMembershipRequest.Status.PENDING


def test_pending_after_questionnaire_approved_completes(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )

    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    result, eligibility = advance_application(app)
    assert result.status == OrganizationMembershipRequest.Status.COMPLETED
    assert eligibility.allowed is True


def test_terminal_application_does_not_advance(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )
    result, _eligibility = advance_application(app)
    assert result.status == OrganizationMembershipRequest.Status.REJECTED
