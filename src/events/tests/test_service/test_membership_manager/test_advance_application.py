"""Tests for advance_application state-advance-on-read helper."""

import typing as t

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


# ---- B2: only explicitly-terminal reasons auto-reject; recoverable blocks stay PENDING ----


def test_pending_with_pending_whitelist_request_stays_pending(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """WHITELIST_PENDING is a recoverable wait state — a read must NOT reject the row."""
    from unittest.mock import patch

    from events.models import Blacklist, WhitelistRequest
    from events.service.membership_manager import MembershipNextStep

    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    pending_request = WhitelistRequest(organization=organization, user=user, status=WhitelistRequest.Status.PENDING)
    with (
        patch("events.service.blacklist_service.check_user_hard_blacklisted", return_value=False),
        patch(
            "events.service.blacklist_service.get_fuzzy_blacklist_matches",
            return_value=[(Blacklist(organization=organization, first_name="Jane", last_name="Doe"), 90)],
        ),
        patch("events.service.whitelist_service.is_user_whitelisted", return_value=False),
        patch("events.service.whitelist_service.get_whitelist_request", return_value=pending_request),
    ):
        result, eligibility = advance_application(app)

    assert result.status == OrganizationMembershipRequest.Status.PENDING
    assert eligibility.allowed is False
    assert eligibility.next_step == MembershipNextStep.WAIT_FOR_WHITELIST_APPROVAL


def test_pending_with_org_made_private_stays_pending(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """ORG_NOT_VISIBLE can be temporary (org flipped private) — must not auto-reject."""
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save(update_fields=["visibility"])

    result, eligibility = advance_application(app)

    assert result.status == OrganizationMembershipRequest.Status.PENDING
    assert eligibility.allowed is False


def test_pending_with_paused_membership_stays_pending(
    user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """MEMBERSHIP_PAUSED is staff-recoverable — must not auto-reject the application."""
    OrganizationMember.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )
    result, eligibility = advance_application(app)

    assert result.status == OrganizationMembershipRequest.Status.PENDING
    assert eligibility.allowed is False


def test_pending_with_terminal_questionnaire_failure_rejects_and_notifies(
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """MEMBERSHIP_QUESTIONNAIRE_FAILED (no retake) IS terminal: reject + notify the user."""
    from notifications.enums import NotificationType
    from notifications.signals import notification_requested

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
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )
    app = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.PENDING,
    )

    received: list[dict[str, object]] = []

    def _collect(sender: object, **kwargs: object) -> None:
        received.append(kwargs)

    notification_requested.connect(_collect)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            result, eligibility = advance_application(app)
    finally:
        notification_requested.disconnect(_collect)

    assert result.status == OrganizationMembershipRequest.Status.REJECTED
    assert eligibility.allowed is False
    assert any(kw.get("notification_type") == NotificationType.MEMBERSHIP_REQUEST_REJECTED for kw in received)
