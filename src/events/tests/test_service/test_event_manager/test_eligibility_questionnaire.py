"""Tests for questionnaire gate eligibility checks."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    OrganizationMember,
    OrganizationQuestionnaire,
)
from events.service.event_manager import EligibilityService, NextStep, Reasons
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


# --- Test Cases for Questionnaire Gate ---


def test_questionnaire_is_missing(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if a required questionnaire has not been submitted."""
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [questionnaire.id]


def test_questionnaire_is_pending_review(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their submission has not yet been evaluated."""
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_PENDING_REVIEW
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION
    assert eligibility.questionnaires_pending_review == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_rejected(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 1
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_FAILED
    assert eligibility.next_step is None
    assert eligibility.questionnaires_failed == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_rejected_and_can_retake_after_time(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected, but can retake after a certain time."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 2
    org_questionnaire.questionnaire.can_retake_after = timedelta(hours=1)
    org_questionnaire.questionnaire.save()

    rejected_evaluation.submission.submitted_at = timezone.now() - timedelta(hours=2)
    rejected_evaluation.submission.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_rejected_and_must_wait_to_retake(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected and must wait to retake."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 2
    org_questionnaire.questionnaire.can_retake_after = timedelta(hours=23, minutes=59, seconds=59)
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_FAILED
    assert eligibility.next_step == NextStep.WAIT_TO_RETAKE_QUESTIONNAIRE


def test_questionnaire_is_rejected_and_can_retake_immediately(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected, but can retake immediately."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 2
    org_questionnaire.questionnaire.can_retake_after = None
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_approved(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is allowed if their evaluation was approved."""
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Questionnaire max_submission_age ---


def test_questionnaire_approved_no_expiration(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is allowed when max_submission_age is not set (no expiration)."""
    org_questionnaire.max_submission_age = None
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_questionnaire_approved_within_max_submission_age(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is allowed when approval is within max_submission_age."""
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    # Evaluation was just created, so it's within the 30-day window
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_questionnaire_approved_but_expired(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied when approval has expired (older than max_submission_age)."""
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    # Set the evaluation's updated_at to 31 days ago (expired)
    expired_time = timezone.now() - timedelta(days=31)
    QuestionnaireEvaluation.objects.filter(pk=approved_evaluation.pk).update(updated_at=expired_time)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [org_questionnaire.questionnaire_id]


def test_questionnaire_approved_just_before_expiration(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is allowed when approval is just before the expiration boundary."""
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    # Set the evaluation's updated_at to 29 days and 23 hours ago (just before expiration)
    just_before_expiry = timezone.now() - timedelta(days=29, hours=23)
    QuestionnaireEvaluation.objects.filter(pk=approved_evaluation.pk).update(updated_at=just_before_expiry)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Still within the 30-day window
    assert eligibility.allowed is True


def test_questionnaire_pending_review_ignores_max_submission_age(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Pending review submissions are not affected by max_submission_age."""
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    # Submission has no evaluation yet (pending review)
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should fail for pending review, not expiration
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_PENDING_REVIEW


def test_questionnaire_rejected_ignores_max_submission_age(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Rejected evaluations are not affected by max_submission_age."""
    org_questionnaire.max_submission_age = timedelta(days=30)
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)
    org_questionnaire.questionnaire.max_attempts = 1
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should fail for rejection, not expiration
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_FAILED


def test_questionnaire_expired_submission_can_be_retaken(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """When a submission expires, the user must complete the questionnaire again."""
    org_questionnaire.max_submission_age = timedelta(hours=1)
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    # Set the evaluation's updated_at to 2 hours ago (expired)
    expired_time = timezone.now() - timedelta(hours=2)
    QuestionnaireEvaluation.objects.filter(pk=approved_evaluation.pk).update(updated_at=expired_time)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    # The questionnaire is listed as missing, meaning the user should complete it again
    assert eligibility.questionnaires_missing == [org_questionnaire.questionnaire_id]


# --- Test Cases for Questionnaire Members Exempt ---


def test_questionnaire_members_exempt_allows_member_without_submission(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Member is allowed when questionnaire has members_exempt=True and no submission exists."""
    org_questionnaire.members_exempt = True
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_questionnaire_members_exempt_allows_member_with_pending_review(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Member is allowed when questionnaire has members_exempt=True even with pending review."""
    org_questionnaire.members_exempt = True
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_questionnaire_members_exempt_allows_member_with_rejected_evaluation(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Member is allowed when questionnaire has members_exempt=True even with rejected evaluation."""
    org_questionnaire.members_exempt = True
    org_questionnaire.save()
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 1
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_questionnaire_members_exempt_still_requires_non_member(
    public_user: RevelUser,
    public_event: Event,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Non-member must still complete questionnaire even when members_exempt=True."""
    org_questionnaire.members_exempt = True
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [questionnaire.id]


def test_questionnaire_members_exempt_false_requires_member_to_complete(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Member must complete questionnaire when members_exempt=False (default)."""
    # members_exempt defaults to False, but let's be explicit
    org_questionnaire.members_exempt = False
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.questionnaires_missing == [questionnaire.id]


def test_questionnaire_members_exempt_inactive_member_not_exempt(
    member_user: RevelUser,
    public_event: Event,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Inactive member (paused/cancelled) is NOT exempt from questionnaire."""
    # Create membership with inactive status
    OrganizationMember.objects.create(
        organization=public_event.organization,
        user=member_user,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    org_questionnaire.members_exempt = True
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.questionnaires_missing == [questionnaire.id]
