"""Tests for ApplicationStatusGate."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMembershipRequest, OrganizationQuestionnaire
from events.service.membership_manager import MembershipEligibilityService
from events.service.membership_manager.enums import MembershipNextStep, MembershipReasonCode, Reasons
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


def test_rejected_application_blocks_with_reapply_next_step(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.APPLICATION_REJECTED)
    assert result.reason_code == MembershipReasonCode.APPLICATION_REJECTED
    # REAPPLY signals the recourse: a fresh POST /apply supersedes this row (B1).
    assert result.next_step == MembershipNextStep.REAPPLY


def test_cancelled_application_does_not_block(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.CANCELLED,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True  # downstream gates allow; user may re-apply


def test_pending_application_falls_through(user: RevelUser, organization: Organization, tier: MembershipTier) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    # No further gates block (no questionnaire/approval configured) → allowed.
    assert result.allowed is True


# ---- REJECTED whose cause is STILL terminal must not advertise REAPPLY ----


def _membership_questionnaire(organization: Organization) -> Questionnaire:
    """Attach a MEMBERSHIP questionnaire as the org default and return it."""
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])
    return q


def _rejected_submission(user: RevelUser, questionnaire: Questionnaire, *, days_ago: int = 0) -> None:
    """Create a READY submission carrying a REJECTED evaluation."""
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now() - timedelta(days=days_ago),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )


def test_rejected_by_terminal_questionnaire_failure_blocks_without_next_step(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """REAPPLY here would be a lie: the fresh row is auto-rejected (and re-notified) on first read.

    The questionnaire has no retake policy, so the very verdict that rejected the
    row still stands — surface it, with next_step=None so the controller 403s.
    """
    q = _membership_questionnaire(organization)
    _rejected_submission(user, q)
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.MEMBERSHIP_QUESTIONNAIRE_FAILED
    assert result.next_step is None
    assert result.questionnaire_id == q.pk
    # Still points at the row the user would otherwise be told to supersede.
    assert result.application_id == app.pk


def test_rejected_at_attempts_cap_blocks_without_next_step(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """The attempts cap is the other terminal code: no retake can ever clear it."""
    q = _membership_questionnaire(organization)
    q.can_retake_after = timedelta(hours=1)
    q.max_attempts = 1
    q.save(update_fields=["can_retake_after", "max_attempts"])
    _rejected_submission(user, q, days_ago=1)  # cooldown already elapsed
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.MEMBERSHIP_QUESTIONNAIRE_ATTEMPTS_EXHAUSTED
    assert result.next_step is None


def test_rejected_with_attempts_remaining_still_offers_reapply(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """A recoverable questionnaire state keeps the documented re-apply path open."""
    q = _membership_questionnaire(organization)
    q.can_retake_after = timedelta(hours=1)
    q.max_attempts = 3
    q.save(update_fields=["can_retake_after", "max_attempts"])
    _rejected_submission(user, q, days_ago=1)  # cooldown elapsed, attempts left
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.reason_code == MembershipReasonCode.APPLICATION_REJECTED
    assert result.next_step == MembershipNextStep.REAPPLY
    assert result.application_id == app.pk


def test_rejected_during_retake_cooldown_still_offers_reapply(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """A cooldown expires into a retake, so the rejection cause is not terminal."""
    q = _membership_questionnaire(organization)
    q.can_retake_after = timedelta(days=7)
    q.save(update_fields=["can_retake_after"])
    _rejected_submission(user, q)
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.reason_code == MembershipReasonCode.APPLICATION_REJECTED
    assert result.next_step == MembershipNextStep.REAPPLY
