"""Tests for MembershipGate's "already on the way in" states on members-only events.

A non-member whose membership attempt is waiting on staff (approval or questionnaire
review) sees a wait state instead of BECOME_MEMBER. The verdict is delegated to
``MembershipEligibilityService`` so the event never contradicts the org membership page.
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


@pytest.fixture(autouse=True)
def public_org(organization: Organization) -> None:
    """Make the org visible: the membership state machine blocks first on ORG_NOT_VISIBLE."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])


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


def _tier(
    organization: Organization,
    *,
    oq: OrganizationQuestionnaire | None = None,
    requires_approval: bool | None = None,
    name: str = "Gold",
) -> MembershipTier:
    return MembershipTier.objects.create(
        organization=organization,
        name=name,
        membership_questionnaire=oq,
        requires_membership_approval=requires_approval,
    )


def _require_approval(organization: Organization) -> None:
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])


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


def test_pending_tierless_application_requiring_approval_waits_for_approval(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """A tier-less PENDING application on an approval-required org is application-pending."""
    org = members_only_event.organization
    _require_approval(org)
    _apply(public_user, org)
    _assert_pending_application(_check(public_user, members_only_event))


def test_pending_application_with_tier_approval_override_waits_for_approval(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """The tier's requires_membership_approval=True applies even when the org default is False."""
    org = members_only_event.organization
    tier = _tier(org, requires_approval=True)
    _apply(public_user, org, tier=tier)
    _assert_pending_application(_check(public_user, members_only_event))


def test_pending_application_with_approved_questionnaire_waits_for_approval(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """Questionnaire approved + approval required -> only staff approval is left."""
    org = members_only_event.organization
    _require_approval(org)
    oq = _membership_oq(org)
    tier = _tier(org, oq=oq)
    _submit(public_user, oq, evaluation=EvalStatus.APPROVED)
    _apply(public_user, org, tier=tier)
    _assert_pending_application(_check(public_user, members_only_event))


@pytest.mark.parametrize("evaluation", [None, EvalStatus.PENDING_REVIEW])
def test_pending_application_with_questionnaire_under_review_waits_for_evaluation(
    public_user: RevelUser, members_only_event: Event, evaluation: str | None
) -> None:
    """The questionnaire gate runs before manual approval, so review is what the user waits on."""
    org = members_only_event.organization
    _require_approval(org)
    oq = _membership_oq(org)
    tier = _tier(org, oq=oq)
    _submit(public_user, oq, evaluation=evaluation)
    _apply(public_user, org, tier=tier)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


def test_pending_application_with_unsubmitted_questionnaire_falls_through(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """The user still has to submit the questionnaire (SUBMIT_QUESTIONNAIRE) -> BECOME_MEMBER."""
    org = members_only_event.organization
    _require_approval(org)
    tier = _tier(org, oq=_membership_oq(org))
    _apply(public_user, org, tier=tier)
    _assert_become_member(_check(public_user, members_only_event))


def test_pending_application_with_all_gates_passing_falls_through(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """Nothing is awaiting staff (no approval, no questionnaire) -> BECOME_MEMBER."""
    org = members_only_event.organization
    tier = _tier(org, requires_approval=False)
    _apply(public_user, org, tier=tier)
    _assert_become_member(_check(public_user, members_only_event))


def test_rejected_application_with_pending_review_falls_through(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """A REJECTED latest application (REAPPLY) wins over a still-pending evaluation."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    tier = _tier(org, oq=oq)
    _submit(public_user, oq, evaluation=EvalStatus.PENDING_REVIEW)
    _apply(public_user, org, tier=tier, status=MembershipRequestStatus.REJECTED)
    _assert_become_member(_check(public_user, members_only_event))


def test_cancelled_application_with_pending_review_still_waits_for_evaluation(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """ApplicationStatusGate only blocks REJECTED, so CANCELLED + pending review is questionnaire-pending."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    tier = _tier(org, oq=oq)
    _submit(public_user, oq, evaluation=EvalStatus.PENDING_REVIEW)
    _apply(public_user, org, tier=tier, status=MembershipRequestStatus.CANCELLED)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


@pytest.mark.parametrize(
    "status",
    [s for s in MembershipRequestStatus.values if s != MembershipRequestStatus.PENDING],
)
def test_non_pending_application_without_submission_falls_through(
    public_user: RevelUser, members_only_event: Event, status: str
) -> None:
    """Resolved applications with nothing under review do not block BECOME_MEMBER."""
    _apply(public_user, members_only_event.organization, status=status)
    _assert_become_member(_check(public_user, members_only_event))


def test_pending_application_to_other_org_is_ignored(
    public_user: RevelUser, members_only_event: Event, organization_owner_user: RevelUser
) -> None:
    """A PENDING application to a different organization does not affect this event."""
    other = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    _require_approval(other)
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


# --- Standalone questionnaire submissions (no application) ---


@pytest.mark.parametrize("evaluation", [None, EvalStatus.PENDING_REVIEW])
def test_standalone_submission_to_tier_override_waits_for_evaluation(
    public_user: RevelUser, members_only_event: Event, evaluation: str | None
) -> None:
    """A submission with no application, resolved by a tier override, is questionnaire-pending."""
    org = members_only_event.organization
    oq = _membership_oq(org, name="Tier Q")
    _tier(org, oq=oq)
    assert org.default_membership_questionnaire_id is None
    _submit(public_user, oq, evaluation=evaluation)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


@pytest.mark.parametrize("evaluation", [None, EvalStatus.PENDING_REVIEW])
def test_standalone_submission_to_inherited_default_waits_for_evaluation(
    public_user: RevelUser, members_only_event: Event, evaluation: str | None
) -> None:
    """A tier that inherits the org default questionnaire resolves to it."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _tier(org)
    _submit(public_user, oq, evaluation=evaluation)
    _assert_pending_questionnaire(_check(public_user, members_only_event))


def test_default_questionnaire_overridden_by_every_tier_falls_through(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """If every tier overrides the default and no tier-less application exists, the default is unreachable."""
    org = members_only_event.organization
    default_oq = _membership_oq(org, name="Default Q")
    _set_default(org, default_oq)
    # The org ships with an auto-created default tier; override it too.
    for i, tier in enumerate(MembershipTier.objects.filter(organization=org)):
        tier.membership_questionnaire = _membership_oq(org, name=f"Tier Q {i}")
        tier.save(update_fields=["membership_questionnaire"])
    _tier(org, oq=_membership_oq(org, name="Gold Q"), name="Gold")
    _submit(public_user, default_oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_latest_rejected_submission_ignores_older_pending(public_user: RevelUser, members_only_event: Event) -> None:
    """Only the latest submission matters: newer REJECTED beats an older pending one."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _tier(org)
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
    _tier(org)
    _submit(public_user, oq, evaluation=EvalStatus.APPROVED)
    _assert_become_member(_check(public_user, members_only_event))


def test_questionnaire_without_requires_evaluation_is_ignored(
    public_user: RevelUser, members_only_event: Event
) -> None:
    """A membership questionnaire that does not require evaluation never yields a wait state."""
    org = members_only_event.organization
    oq = _membership_oq(org, requires_evaluation=False)
    _set_default(org, oq)
    _tier(org)
    _submit(public_user, oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_unrelated_questionnaire_submission_is_ignored(public_user: RevelUser, members_only_event: Event) -> None:
    """A pending submission to a non-membership questionnaire is not a membership wait state."""
    org = members_only_event.organization
    _tier(org)
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
    org = members_only_event.organization
    _tier(org)
    _submit(public_user, _membership_oq(org, name="Orphan"))
    _assert_become_member(_check(public_user, members_only_event))


def test_other_orgs_membership_questionnaire_is_ignored(
    public_user: RevelUser, members_only_event: Event, organization_owner_user: RevelUser
) -> None:
    """A pending submission to another organization's membership questionnaire is ignored."""
    _tier(members_only_event.organization)
    other = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
    oq = _membership_oq(other, name="Other Q")
    _set_default(other, oq)
    _tier(other, name="Other tier")
    _submit(public_user, oq)
    _assert_become_member(_check(public_user, members_only_event))


def test_draft_submission_is_not_a_pending_review(public_user: RevelUser, members_only_event: Event) -> None:
    """A DRAFT (unsubmitted) submission must not count as awaiting review."""
    org = members_only_event.organization
    oq = _membership_oq(org)
    _set_default(org, oq)
    _tier(org)
    QuestionnaireSubmission.objects.create(
        user=public_user,
        questionnaire=oq.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
    )
    _assert_become_member(_check(public_user, members_only_event))


# --- Query budget ---


def test_active_member_check_makes_no_queries(
    member_user: RevelUser, members_only_event: Event, django_assert_num_queries: t.Any
) -> None:
    """Active members never touch the lazy membership lookup."""
    OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=member_user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    service = EligibilityService(user=member_user, event=members_only_event)
    with django_assert_num_queries(0):
        assert service.check_eligibility().allowed is True


def test_non_member_without_submissions_costs_one_query(
    public_user: RevelUser, members_only_event: Event, django_assert_num_queries: t.Any
) -> None:
    """With no submissions the tier lookup is skipped: only the application query runs."""
    _tier(members_only_event.organization)
    service = EligibilityService(user=public_user, event=members_only_event)
    with django_assert_num_queries(1):
        assert service.pending_membership_step is None


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
