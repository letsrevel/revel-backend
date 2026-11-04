"""Tests for MembershipGate's "already on the way in" states on members-only events.

A non-member with a PENDING membership application, or a membership questionnaire
submission awaiting review, must see a wait state instead of BECOME_MEMBER.
"""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipRequestStatus,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
from events.service.event_manager import EligibilityService, NextStep, Reasons
from events.service.event_manager.enums import ReasonCode
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

EvalStatus = QuestionnaireEvaluation.QuestionnaireEvaluationStatus


def _membership_oq(
    organization: Organization, *, requires_evaluation: bool = True, name: str = "Membership Q"
) -> OrganizationQuestionnaire:
    q = Questionnaire.objects.create(name=name, status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        requires_evaluation=requires_evaluation,
    )


def _set_default(organization: Organization, oq: OrganizationQuestionnaire) -> None:
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])


def _submit(
    user: RevelUser,
    oq: OrganizationQuestionnaire,
    *,
    evaluation: str | None = None,
    age_minutes: int = 0,
) -> QuestionnaireSubmission:
    """READY submission, optionally with an evaluation of the given status."""
    sub = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=oq.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now() - timedelta(minutes=age_minutes),
    )
    if evaluation is not None:
        QuestionnaireEvaluation.objects.create(submission=sub, status=evaluation)
    return sub


def _apply(
    user: RevelUser,
    organization: Organization,
    *,
    status: str = MembershipRequestStatus.PENDING,
    tier: MembershipTier | None = None,
) -> OrganizationMembershipRequest:
    return OrganizationMembershipRequest.objects.create(user=user, organization=organization, status=status, tier=tier)


def _check(user: RevelUser, event: Event) -> t.Any:
    return EligibilityService(user=user, event=event).check_eligibility()


def _assert_pending_application(result: t.Any) -> None:
    assert result.allowed is False
    assert result.reason == Reasons.MEMBERSHIP_APPLICATION_PENDING
    assert result.reason_code == ReasonCode.MEMBERSHIP_APPLICATION_PENDING
    assert result.next_step == NextStep.WAIT_FOR_MEMBERSHIP_APPROVAL


def _assert_pending_questionnaire(result: t.Any) -> None:
    assert result.allowed is False
    assert result.reason == Reasons.MEMBERSHIP_QUESTIONNAIRE_PENDING_REVIEW
    assert result.reason_code == ReasonCode.MEMBERSHIP_QUESTIONNAIRE_PENDING_REVIEW
    assert result.next_step == NextStep.WAIT_FOR_MEMBERSHIP_QUESTIONNAIRE_EVALUATION


def _assert_become_member(result: t.Any) -> None:
    assert result.allowed is False
    assert result.reason == Reasons.MEMBERS_ONLY
    assert result.reason_code == ReasonCode.MEMBERS_ONLY
    assert result.next_step == NextStep.BECOME_MEMBER


# --- Pending application ---


def test_pending_tierless_application_waits_for_approval(public_user: RevelUser, members_only_event: Event) -> None:
    """A tier-less PENDING application yields the application-pending wait state."""
    _apply(public_user, members_only_event.organization)
    _assert_pending_application(_check(public_user, members_only_event))


def test_pending_tiered_application_waits_for_approval(public_user: RevelUser, members_only_event: Event) -> None:
    """A PENDING application targeting a tier yields the same wait state."""
    tier = MembershipTier.objects.create(organization=members_only_event.organization, name="Gold")
    _apply(public_user, members_only_event.organization, tier=tier)
    _assert_pending_application(_check(public_user, members_only_event))


def test_pending_application_applies_even_when_requests_not_accepted(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """Pending state is shown regardless of accept_membership_requests."""
    org = members_only_event.organization
    org.accept_membership_requests = False
    org.save(update_fields=["accept_membership_requests"])
    _apply(public_user, org)
    _assert_pending_application(_check(public_user, members_only_event))


def test_pending_application_beats_pending_questionnaire(public_user: RevelUser, members_only_event: Event) -> None:
    """Application-pending takes precedence over questionnaire-pending."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _submit(public_user, oq)
    _apply(public_user, org)
    _assert_pending_application(_check(public_user, members_only_event))


@pytest.mark.parametrize(
    "status",
    [s for s in MembershipRequestStatus.values if s != MembershipRequestStatus.PENDING],
)
def test_non_pending_application_falls_through_to_become_member(
    public_user: RevelUser, members_only_event: Event, status: str
) -> None:
    """Resolved applications (any non-PENDING status) do not block BECOME_MEMBER."""
    _apply(public_user, members_only_event.organization, status=status)
    _assert_become_member(_check(public_user, members_only_event))


def test_pending_application_to_other_org_is_ignored(
    public_user: RevelUser, members_only_event: Event, organization_owner_user: RevelUser
) -> None:
    """A PENDING application to a different organization does not affect this event."""
    other = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    _apply(public_user, other)
    _assert_become_member(_check(public_user, members_only_event))


def test_no_application_and_requests_disabled_has_no_next_step(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """Without pending state and with requests disabled, next_step stays None."""
    org = members_only_event.organization
    org.accept_membership_requests = False
    org.save(update_fields=["accept_membership_requests"])
    result = _check(public_user, members_only_event)
    assert result.reason == Reasons.MEMBERS_ONLY
    assert result.next_step is None


# --- Pending questionnaire ---


@pytest.mark.parametrize("evaluation", [None, EvalStatus.PENDING_REVIEW])
def test_default_questionnaire_pending_review_waits_for_evaluation(
    public_user: RevelUser, members_only_event: Event, evaluation: str | None
) -> None:
    """READY submission with no evaluation, or a PENDING_REVIEW one, is a wait state."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _submit(public_user, oq, evaluation=evaluation)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


@pytest.mark.parametrize("evaluation", [None, EvalStatus.PENDING_REVIEW])
def test_tier_questionnaire_pending_review_waits_for_evaluation(
    public_user: RevelUser, members_only_event: Event, evaluation: str | None
) -> None:
    """A tier-level membership questionnaire (no org default) counts too."""
    org = members_only_event.organization
    oq = _membership_oq(org, name="Tier Q")
    MembershipTier.objects.create(organization=org, name="Student", membership_questionnaire=oq)
    assert org.default_membership_questionnaire_id is None
    _submit(public_user, oq, evaluation=evaluation)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


def test_questionnaire_pending_applies_even_when_requests_not_accepted(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """Questionnaire-pending is shown regardless of accept_membership_requests."""
    org = members_only_event.organization
    org.accept_membership_requests = False
    org.save(update_fields=["accept_membership_requests"])
    oq = _membership_oq(org)
    _set_default(org, oq)
    _submit(public_user, oq)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


def test_latest_rejected_submission_ignores_older_pending(public_user: RevelUser, members_only_event: Event) -> None:
    """Only the latest submission matters: newer REJECTED beats an older pending one."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _submit(public_user, oq, age_minutes=60)
    _submit(public_user, oq, evaluation=EvalStatus.REJECTED, age_minutes=1)
    _assert_become_member(_check(public_user, members_only_event))


def test_latest_approved_submission_falls_through_to_become_member(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """An approved-but-not-joined user is not shown a wait state."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _submit(public_user, oq, evaluation=EvalStatus.APPROVED)
    _assert_become_member(_check(public_user, members_only_event))


def test_questionnaire_without_requires_evaluation_is_ignored(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """A membership questionnaire that does not require evaluation never yields a wait state."""
    org = members_only_event.organization
    oq = _membership_oq(org, requires_evaluation=False)
    _set_default(org, oq)
    _submit(public_user, oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_unrelated_questionnaire_submission_is_ignored(public_user: RevelUser, members_only_event: Event) -> None:
    """A pending submission to a non-membership questionnaire is not a membership wait state."""
    org = members_only_event.organization
    q = Questionnaire.objects.create(name="Admission", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=org,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        requires_evaluation=True,
    )
    _submit(public_user, oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_questionnaire_not_linked_to_default_or_tier_is_ignored(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """A MEMBERSHIP-type questionnaire nothing points at does not count."""
    oq = _membership_oq(members_only_event.organization, name="Orphan")
    _submit(public_user, oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_other_orgs_membership_questionnaire_is_ignored(
    public_user: RevelUser, members_only_event: Event, organization_owner_user: RevelUser
) -> None:
    """A pending submission to another organization's membership questionnaire is ignored."""
    other = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    oq = _membership_oq(other, name="Other Q")
    _set_default(other, oq)
    _submit(public_user, oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_draft_submission_is_not_a_pending_review(public_user: RevelUser, members_only_event: Event) -> None:
    """A DRAFT (unsubmitted) submission must not count as awaiting review."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    QuestionnaireSubmission.objects.create(
        user=public_user,
        questionnaire=oq.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
    )
    _assert_become_member(_check(public_user, members_only_event))


# --- Existing (inactive) members are unaffected ---


@pytest.mark.parametrize(
    "status",
    [
        OrganizationMember.MembershipStatus.PAUSED,
        OrganizationMember.MembershipStatus.CANCELLED,
        OrganizationMember.MembershipStatus.BANNED,
    ],
)
def test_inactive_member_with_pending_application_stays_inactive(
    member_user: RevelUser, members_only_event: Event, status: str
) -> None:
    """Pending state is only for users with no member row; inactive members keep MEMBERSHIP_INACTIVE."""
    org = members_only_event.organization
    OrganizationMember.objects.create(organization=org, user=member_user, status=status)
    _apply(member_user, org)
    result = _check(member_user, members_only_event)
    assert result.allowed is False
    assert result.reason == Reasons.MEMBERSHIP_INACTIVE
    assert result.reason_code == ReasonCode.MEMBERSHIP_INACTIVE
    assert result.next_step is None
