"""Tests for application deadline gate and effective_apply_deadline property."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    OrganizationMember,
    OrganizationQuestionnaire,
)
from events.service.event_manager import EligibilityService, NextStep, Reasons
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


# --- Test Cases for Application Deadline Gate ---


def test_apply_deadline_not_set_allows_access(public_user: RevelUser, private_event: Event) -> None:
    """Test that no apply_before deadline allows normal access flow."""
    private_event.apply_before = None
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # Should fail for invitation requirement, not deadline
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.REQUIRES_INVITATION


def test_apply_deadline_not_passed_allows_access(public_user: RevelUser, private_event: Event) -> None:
    """Test that apply_before deadline in the future allows normal access flow."""
    private_event.apply_before = timezone.now() + timedelta(hours=1)
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # Should fail for invitation requirement, not deadline
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert eligibility.next_step == NextStep.REQUEST_INVITATION


def test_apply_deadline_passed_blocks_user_needing_invitation(public_user: RevelUser, private_event: Event) -> None:
    """Test that expired apply_before deadline blocks users who need to request invitation."""
    private_event.apply_before = timezone.now() - timedelta(hours=1)
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.APPLICATION_DEADLINE_PASSED
    assert eligibility.next_step is None


def test_apply_deadline_passed_allows_user_with_invitation(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """Test that expired apply_before deadline allows users who already have invitation."""
    private_event.apply_before = timezone.now() - timedelta(hours=1)
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # User has invitation, so deadline doesn't block them
    assert eligibility.allowed is True


def test_apply_deadline_passed_allows_user_with_pending_request(public_user: RevelUser, private_event: Event) -> None:
    """Test that expired deadline allows users who already submitted a request (even if pending)."""
    private_event.apply_before = timezone.now() - timedelta(hours=1)
    private_event.save()

    # User already submitted an invitation request
    EventInvitationRequest.objects.create(
        event=private_event,
        user=public_user,
        status=EventInvitationRequest.InvitationRequestStatus.PENDING,
    )

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # User already applied, so deadline passed is not the reason for blocking
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.INVITATION_REQUEST_PENDING
    assert eligibility.next_step == NextStep.WAIT_FOR_INVITATION_APPROVAL


def test_apply_deadline_passed_allows_user_with_rejected_request(public_user: RevelUser, private_event: Event) -> None:
    """Test that expired deadline allows users who had their request rejected (already applied)."""
    private_event.apply_before = timezone.now() - timedelta(hours=1)
    private_event.save()

    # User had a rejected invitation request
    EventInvitationRequest.objects.create(
        event=private_event,
        user=public_user,
        status=EventInvitationRequest.InvitationRequestStatus.REJECTED,
    )

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # User already applied, so deadline passed is not the reason for blocking
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.INVITATION_REQUEST_REJECTED


def test_apply_deadline_waived_by_invitation(public_user: RevelUser, private_event: Event) -> None:
    """Test that invitation with waives_apply_deadline=True bypasses deadline."""
    private_event.apply_before = timezone.now() - timedelta(hours=1)
    private_event.save()

    # Create invitation that waives apply deadline
    EventInvitation.objects.create(user=public_user, event=private_event, waives_apply_deadline=True)

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_apply_deadline_passed_blocks_user_needing_questionnaire(
    public_user: RevelUser,
    public_event: Event,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Test that expired deadline blocks users who need to complete questionnaire."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.APPLICATION_DEADLINE_PASSED
    assert eligibility.next_step is None


def test_apply_deadline_passed_allows_user_with_approved_questionnaire(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Test that expired deadline allows users who already passed questionnaire."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # User already passed questionnaire, so they're allowed
    assert eligibility.allowed is True


def test_apply_deadline_passed_allows_user_with_pending_evaluation(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Test that expired deadline allows users with pending questionnaire evaluation."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # User already submitted questionnaire (pending review), so deadline doesn't block
    # But they're blocked for pending review
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_PENDING_REVIEW


def test_apply_deadline_passed_blocks_user_with_failed_questionnaire_needing_retake(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Test that expired deadline blocks users who failed questionnaire and need to retake."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()
    org_questionnaire.events.add(public_event)
    org_questionnaire.questionnaire.max_attempts = 2  # Allow retakes
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # User failed and needs to retake, but deadline passed
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.APPLICATION_DEADLINE_PASSED


def test_apply_deadline_ignored_for_public_event_without_questionnaire(
    public_user: RevelUser, public_event: Event
) -> None:
    """Test that apply deadline doesn't block users for public events without questionnaires."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Public event without questionnaire - user doesn't need to apply for anything
    assert eligibility.allowed is True


def test_apply_deadline_ignored_for_members_only_event_without_questionnaire(
    member_user: RevelUser, members_only_event: Event, organization_membership: OrganizationMember
) -> None:
    """Test that apply deadline doesn't block members for members-only events without questionnaires."""
    # organization_membership fixture already creates membership in the same organization
    # (both members_only_event and organization_membership use the same organization fixture)

    members_only_event.apply_before = timezone.now() - timedelta(hours=1)
    members_only_event.save()

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    # Member doesn't need to apply for anything
    assert eligibility.allowed is True


def test_apply_deadline_questionnaire_waived_by_invitation(
    public_user: RevelUser,
    public_event: Event,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Test that invitation waiving questionnaire means user doesn't need to apply."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()
    org_questionnaire.events.add(public_event)

    # Create invitation that waives questionnaire requirement
    EventInvitation.objects.create(user=public_user, event=public_event, waives_questionnaire=True)

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Questionnaire is waived, so user doesn't need to apply
    assert eligibility.allowed is True


def test_apply_deadline_members_exempt_questionnaire(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Test that members exempt from questionnaire aren't blocked by deadline."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()
    org_questionnaire.members_exempt = True
    org_questionnaire.save()
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Member is exempt from questionnaire, so they don't need to apply
    assert eligibility.allowed is True


# --- Test Cases for effective_apply_deadline Property ---


def test_effective_apply_deadline_returns_apply_before_when_set(private_event: Event) -> None:
    """Test that effective_apply_deadline returns apply_before when explicitly set."""
    deadline = timezone.now() + timedelta(days=5)
    private_event.apply_before = deadline
    private_event.save()

    assert private_event.effective_apply_deadline == deadline


def test_effective_apply_deadline_returns_event_start_when_not_set(private_event: Event) -> None:
    """Test that effective_apply_deadline falls back to event start when apply_before is None."""
    private_event.apply_before = None
    private_event.save()

    assert private_event.effective_apply_deadline == private_event.start


def test_apply_deadline_fallback_to_event_start_blocks_user(public_user: RevelUser, private_event: Event) -> None:
    """Test that when apply_before is None, event start is used as deadline fallback."""
    # Set event start to the past (deadline has passed)
    private_event.start = timezone.now() - timedelta(hours=1)
    private_event.end = timezone.now() + timedelta(hours=23)
    private_event.apply_before = None
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # User needs invitation but deadline (event start) has passed
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.APPLICATION_DEADLINE_PASSED


def test_apply_deadline_fallback_allows_user_with_invitation(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """Test that deadline fallback still allows users who already have invitation."""
    # Set event start to the past (deadline has passed)
    private_event.start = timezone.now() - timedelta(hours=1)
    private_event.end = timezone.now() + timedelta(hours=23)
    private_event.apply_before = None
    private_event.save()

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    # User has invitation, deadline doesn't block them
    assert eligibility.allowed is True
