"""Integration tests covering spec scenarios S1–S7 through the full gate chain."""

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
from events.service.membership_manager import MembershipEligibilityService, advance_application
from events.service.membership_manager.enums import MembershipNextStep, MembershipReasonCode, Reasons
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def _make_membership_questionnaire(
    organization: Organization, name: str
) -> tuple[Questionnaire, OrganizationQuestionnaire]:
    q = Questionnaire.objects.create(name=name, status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    return q, oq


def _pass_questionnaire(user: RevelUser, questionnaire: Questionnaire) -> None:
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )


def test_s1_open_community_free_passes(user: RevelUser, organization: Organization) -> None:
    """S1: no gates configured → free join passes."""
    tier = MembershipTier.objects.create(organization=organization, name="Member")
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    result = service.check_eligibility()
    assert result.allowed is True
    assert result.next_step is None


def test_s2_one_org_wide_questionnaire(user: RevelUser, organization: Organization) -> None:
    """S2: single MEMBERSHIP questionnaire applies to all tiers."""
    tier_a = MembershipTier.objects.create(organization=organization, name="A")
    tier_b = MembershipTier.objects.create(organization=organization, name="B")
    q, oq = _make_membership_questionnaire(organization, "Org intake")
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    # Both tiers initially require the questionnaire
    for tier in (tier_a, tier_b):
        service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
        result = service.check_eligibility()
        assert result.next_step == MembershipNextStep.SUBMIT_QUESTIONNAIRE

    # Pass once → both tiers unlocked
    _pass_questionnaire(user, q)
    for tier in (tier_a, tier_b):
        service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
        result = service.check_eligibility()
        assert result.allowed is True


def test_s3_per_tier_questionnaire(user: RevelUser, organization: Organization) -> None:
    """S3: each tier has its own questionnaire."""
    visitor = MembershipTier.objects.create(organization=organization, name="Visitor")
    student = MembershipTier.objects.create(organization=organization, name="Student")
    q_student, oq_student = _make_membership_questionnaire(organization, "Safety form")
    student.membership_questionnaire = oq_student
    student.save(update_fields=["membership_questionnaire"])

    # Visitor: no gate
    s = MembershipEligibilityService(user=user, organization=organization, tier=visitor)
    assert s.check_eligibility().allowed is True

    # Student: requires the safety form
    s = MembershipEligibilityService(user=user, organization=organization, tier=student)
    assert s.check_eligibility().questionnaire_id == q_student.id


def test_s4_asymmetric_paid_gated(user: RevelUser, organization: Organization) -> None:
    """S4: free tier open, paid tier verification required."""
    free_tier = MembershipTier.objects.create(organization=organization, name="Free")
    paid_tier = MembershipTier.objects.create(organization=organization, name="Paid")
    _, oq = _make_membership_questionnaire(organization, "Real-human check")
    paid_tier.membership_questionnaire = oq
    paid_tier.save(update_fields=["membership_questionnaire"])

    assert (
        MembershipEligibilityService(user=user, organization=organization, tier=free_tier).check_eligibility().allowed
        is True
    )
    assert (
        MembershipEligibilityService(user=user, organization=organization, tier=paid_tier).check_eligibility().next_step
        == MembershipNextStep.SUBMIT_QUESTIONNAIRE
    )


def test_s5_manual_approval_no_questionnaire(user: RevelUser, organization: Organization) -> None:
    """S5: manual approval gate without any questionnaire.

    With no application on file the verdict is apply-able but flagged
    ``REQUIRES_APPROVAL`` (#787); once the row exists it blocks with a wait.
    """
    tier = MembershipTier.objects.create(organization=organization, name="Member")
    organization.default_requires_membership_approval = True
    organization.save(update_fields=["default_requires_membership_approval"])

    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()
    assert result.allowed is True
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.next_step is None

    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()
    assert result.allowed is False
    assert result.next_step == MembershipNextStep.WAIT_FOR_APPROVAL
    assert result.application_id == app.pk


def test_rejected_then_new_application_runs_gates_fresh(user: RevelUser, organization: Organization) -> None:
    """REJECTED is terminal for THIS row, not for the user.

    Re-applying after REJECTED creates a fresh PENDING row (permitted by the
    partial unique constraint, which only blocks duplicate-PENDING rows).
    The most-recent-per-tier prefetch sees the new PENDING; ApplicationStatusGate
    falls through and the rest of the chain evaluates the fresh attempt from
    scratch.
    """
    tier = MembershipTier.objects.create(organization=organization, name="Standard")

    # 1. Old REJECTED row exists.
    rejected = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    # 2. A fresh PENDING row is permitted (partial unique constraint excludes
    #    non-PENDING statuses).
    new_pending = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    assert new_pending.pk != rejected.pk

    # 3. The service sees the NEW PENDING (newest per tier wins). With no
    #    questionnaire / approval gate configured, eligibility falls through
    #    to ALLOWED — the REJECTED row does NOT short-circuit.
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier)
    assert service.current_application is not None
    assert service.current_application.pk == new_pending.pk
    result = service.check_eligibility()
    assert result.allowed is True


def test_s7_tier_upgrade_re_gates(user: RevelUser, organization: Organization) -> None:
    """S7: existing ACTIVE member at tier A must pass new tier B's questionnaire."""
    tier_a = MembershipTier.objects.create(organization=organization, name="A")
    tier_b = MembershipTier.objects.create(organization=organization, name="B")
    _, oq_b = _make_membership_questionnaire(organization, "Tier B exam")
    tier_b.membership_questionnaire = oq_b
    tier_b.save(update_fields=["membership_questionnaire"])

    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier_a,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    # Upgrade target (tier B) must trigger its own questionnaire
    service = MembershipEligibilityService(user=user, organization=organization, tier=tier_b)
    result = service.check_eligibility()
    assert result.allowed is False
    assert result.next_step == MembershipNextStep.SUBMIT_QUESTIONNAIRE


def test_tier_less_pending_application_signals_wait_for_approval(user: RevelUser, organization: Organization) -> None:
    """#788: a tier-less, plan-less PENDING row is under review, not joinable.

    It passes every gate (approval is NOT required here), so the final allowed
    verdict must carry the wait signal explicitly — otherwise the org page
    renders "Join" while the account hub says "under review".
    """
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    result = MembershipEligibilityService(user=user, organization=organization).check_eligibility()
    assert result.allowed is True
    assert result.next_step == MembershipNextStep.WAIT_FOR_APPROVAL
    assert result.reason == str(Reasons.REQUIRES_APPROVAL)
    assert result.reason_code == MembershipReasonCode.REQUIRES_APPROVAL
    assert result.application_id == app.pk
    assert result.tier_id is None


def test_tier_bearing_pending_application_still_auto_completes(user: RevelUser, organization: Organization) -> None:
    """The #788 shape must stay narrow: tier-bearing free rows still auto-complete.

    Their returned eligibility must not claim "wait for approval" for a row that
    just advanced to COMPLETED.
    """
    tier = MembershipTier.objects.create(organization=organization, name="Member")
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    advanced, eligibility = advance_application(app)
    assert advanced.status == OrganizationMembershipRequest.Status.COMPLETED
    assert eligibility.allowed is True
    assert eligibility.next_step is None
    assert eligibility.reason_code is None


def test_no_application_and_no_approval_stays_silently_allowed(user: RevelUser, organization: Organization) -> None:
    """No annotation leakage: the plain happy path carries no reason/next_step."""
    tier = MembershipTier.objects.create(organization=organization, name="Member")
    result = MembershipEligibilityService(user=user, organization=organization, tier=tier).check_eligibility()
    assert result.allowed is True
    assert result.next_step is None
    assert result.reason is None
    assert result.reason_code is None
    assert result.application_id is None
