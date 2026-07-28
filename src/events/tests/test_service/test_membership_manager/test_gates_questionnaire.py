"""Tests for MembershipQuestionnaireGate."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationQuestionnaire
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


@pytest.fixture
def org_questionnaire(organization: Organization) -> OrganizationQuestionnaire:
    q = Questionnaire.objects.create(name="Member intake", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )


def _attach_to_org(org: Organization, oq: OrganizationQuestionnaire) -> None:
    org.default_membership_questionnaire = oq
    org.save(update_fields=["default_membership_questionnaire"])


def test_no_questionnaire_configured_falls_through(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True


def test_missing_submission_returns_submit_next_step(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    _attach_to_org(organization, org_questionnaire)
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.MEMBERSHIP_QUESTIONNAIRE_MISSING)
    assert result.next_step == MembershipNextStep.SUBMIT_QUESTIONNAIRE
    assert result.questionnaire_id == org_questionnaire.questionnaire_id


def test_pending_evaluation_returns_wait_next_step(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    _attach_to_org(organization, org_questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=org_questionnaire.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.next_step == MembershipNextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION


def test_approved_evaluation_passes(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    _attach_to_org(organization, org_questionnaire)
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=org_questionnaire.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True


def test_rejected_with_retake_cooldown_returns_retry_on(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    _attach_to_org(organization, org_questionnaire)
    q = org_questionnaire.questionnaire
    q.can_retake_after = timedelta(days=1)
    q.save(update_fields=["can_retake_after"])
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.next_step == MembershipNextStep.WAIT_TO_RETAKE_QUESTIONNAIRE
    assert result.retry_on is not None


def test_rejected_without_retake_returns_failed_terminal(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    _attach_to_org(organization, org_questionnaire)
    # can_retake_after is None → no retake
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=org_questionnaire.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.reason == str(Reasons.MEMBERSHIP_QUESTIONNAIRE_FAILED)
    assert result.next_step is None


def _rejected_submission(
    user: RevelUser, questionnaire: Questionnaire, submitted_at: datetime | None = None
) -> QuestionnaireSubmission:
    """Create a READY submission with a REJECTED evaluation."""
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=submitted_at or timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )
    return submission


def test_rejected_at_attempts_cap_returns_attempts_exhausted(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """#810: at the cap the verdict is terminal, not an invitation to submit again.

    Cooldown has elapsed, so before the dedicated code this returned
    SUBMIT_QUESTIONNAIRE — and the submission it invited was guaranteed a 400.
    """
    _attach_to_org(organization, org_questionnaire)
    q = org_questionnaire.questionnaire
    q.can_retake_after = timedelta(hours=1)
    q.max_attempts = 1
    q.save(update_fields=["can_retake_after", "max_attempts"])
    _rejected_submission(user, q, submitted_at=timezone.now() - timedelta(days=1))

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.allowed is False
    assert result.reason_code == MembershipReasonCode.MEMBERSHIP_QUESTIONNAIRE_ATTEMPTS_EXHAUSTED
    assert result.reason == str(Reasons.MEMBERSHIP_QUESTIONNAIRE_ATTEMPTS_EXHAUSTED)
    assert result.next_step is None
    assert result.questionnaire_id == q.pk


def test_attempts_cap_outranks_an_unexpired_retake_cooldown(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """Waiting out the cooldown would change nothing, so don't promise a retry."""
    _attach_to_org(organization, org_questionnaire)
    q = org_questionnaire.questionnaire
    q.can_retake_after = timedelta(days=7)
    q.max_attempts = 1
    q.save(update_fields=["can_retake_after", "max_attempts"])
    _rejected_submission(user, q)

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.reason_code == MembershipReasonCode.MEMBERSHIP_QUESTIONNAIRE_ATTEMPTS_EXHAUSTED
    assert result.next_step is None
    assert result.retry_on is None


def test_attempts_remaining_still_invites_a_new_submission(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """0 < used < max: the CTA stays SUBMIT_QUESTIONNAIRE."""
    _attach_to_org(organization, org_questionnaire)
    q = org_questionnaire.questionnaire
    q.can_retake_after = timedelta(hours=1)
    q.max_attempts = 3
    q.save(update_fields=["can_retake_after", "max_attempts"])
    _rejected_submission(user, q, submitted_at=timezone.now() - timedelta(days=1))

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.allowed is False
    assert result.next_step == MembershipNextStep.SUBMIT_QUESTIONNAIRE
    assert result.reason_code == MembershipReasonCode.MEMBERSHIP_QUESTIONNAIRE_MISSING


def test_unlimited_attempts_never_exhausts(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """max_attempts=0 means unlimited — three rejections still resolve by retake policy."""
    _attach_to_org(organization, org_questionnaire)
    q = org_questionnaire.questionnaire
    assert q.max_attempts == 0  # default: unlimited
    for days_ago in (3, 2, 1):
        _rejected_submission(user, q, submitted_at=timezone.now() - timedelta(days=days_ago))

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    # can_retake_after is None → the pre-existing terminal FAILED verdict, unchanged.
    assert result.reason_code == MembershipReasonCode.MEMBERSHIP_QUESTIONNAIRE_FAILED


def test_stale_approval_ignores_the_attempts_cap(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """An aged-out APPROVED evaluation asks for a resubmission even at the cap.

    Lockstep with ``_validate_resubmission``, which returns early (allowing the
    submission) for a stale approval before it ever reaches the cap check.
    """
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save(update_fields=["max_submission_age"])
    _attach_to_org(organization, org_questionnaire)
    q = org_questionnaire.questionnaire
    q.max_attempts = 1
    q.save(update_fields=["max_attempts"])

    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now() - timedelta(days=90),
    )
    evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )
    QuestionnaireEvaluation.objects.filter(pk=evaluation.pk).update(updated_at=timezone.now() - timedelta(days=90))

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()

    assert result.next_step == MembershipNextStep.SUBMIT_QUESTIONNAIRE


def test_members_exempt_skips_gate_for_existing_active_member(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """When the resolved questionnaire has members_exempt=True, ACTIVE members bypass it."""
    from events.models import OrganizationMember

    org_questionnaire.members_exempt = True
    org_questionnaire.save(update_fields=["members_exempt"])
    _attach_to_org(organization, org_questionnaire)

    other_tier = MembershipTier.objects.create(organization=organization, name="Other")
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=other_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True


def test_tier_questionnaire_overrides_org_default(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q_org = Questionnaire.objects.create(name="Org default", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq_org = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q_org,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    q_tier = Questionnaire.objects.create(name="Tier-only", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq_tier = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q_tier,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq_org
    organization.save(update_fields=["default_membership_questionnaire"])
    tier.membership_questionnaire = oq_tier
    tier.save(update_fields=["membership_questionnaire"])

    # User submits the ORG default, not the tier-specific one → still blocked.
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=q_org,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.questionnaire_id == q_tier.id


def test_questionnaire_without_evaluation_requirement_passes_on_ready_submission(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """When requires_evaluation=False, a READY submission is enough — no eval needed.

    Information-gathering questionnaires gate access without judgment: the user
    submits, the gate passes, no LLM or manual review involved.
    """
    # Arrange
    org_questionnaire.requires_evaluation = False
    org_questionnaire.save(update_fields=["requires_evaluation"])
    _attach_to_org(organization, org_questionnaire)

    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=org_questionnaire.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    # Deliberately NO QuestionnaireEvaluation — must not be required.

    # Act
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()

    # Assert
    assert result.allowed is True


def test_approved_evaluation_expired_by_max_submission_age_requires_resubmit(
    user: RevelUser, organization: Organization, tier: MembershipTier, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """An APPROVED evaluation older than max_submission_age must re-trigger SUBMIT_QUESTIONNAIRE.

    Orgs set max_submission_age to expire stale approvals: even an APPROVED
    evaluation stops counting once it's older than the configured window.
    """
    # Arrange: questionnaire requires resubmission after 30 days.
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save(update_fields=["max_submission_age"])
    _attach_to_org(organization, org_questionnaire)

    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=org_questionnaire.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now() - timedelta(days=90),
    )
    evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )
    # Force-backdate evaluation.updated_at past the max_submission_age window.
    QuestionnaireEvaluation.objects.filter(pk=evaluation.pk).update(updated_at=timezone.now() - timedelta(days=90))

    # Act
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()

    # Assert: stale approval → user is asked to submit again.
    assert result.allowed is False
    assert result.next_step == MembershipNextStep.SUBMIT_QUESTIONNAIRE
    assert result.reason == str(Reasons.MEMBERSHIP_QUESTIONNAIRE_MISSING)
    assert result.questionnaire_id == org_questionnaire.questionnaire_id
