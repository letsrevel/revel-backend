"""Tests for requires_evaluation flag on questionnaire eligibility gates."""

import pytest

from accounts.models import RevelUser
from events.models import (
    Event,
    OrganizationMember,
    OrganizationQuestionnaire,
)
from events.service.event_manager import EligibilityService, Reasons
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


def test_no_evaluation_required_submitted_allows_access(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is allowed when requires_evaluation=False and a READY submission exists (no evaluation needed)."""
    org_questionnaire.requires_evaluation = False
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_no_evaluation_required_missing_submission_blocks(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is blocked when requires_evaluation=False but no submission exists."""
    org_questionnaire.requires_evaluation = False
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING


def test_no_evaluation_required_skips_pending_review_check(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """With requires_evaluation=False, a submission without evaluation is NOT treated as pending review."""
    org_questionnaire.requires_evaluation = False
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    # submitted_submission has no evaluation — normally this would be PENDING_REVIEW
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed, not blocked with QUESTIONNAIRE_PENDING_REVIEW
    assert eligibility.allowed is True


def test_no_evaluation_required_skips_rejected_check(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """With requires_evaluation=False, a rejected evaluation does not block access."""
    org_questionnaire.requires_evaluation = False
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)
    org_questionnaire.questionnaire.max_attempts = 1
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed — rejected evaluation is irrelevant when evaluation isn't required
    assert eligibility.allowed is True
