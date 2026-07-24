"""Controller tests for re-apply after rejection (B1) and submission validation (B3).

Split from test_me_applications.py to respect the 1000-line file limit.
"""

import uuid

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
)
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


def _client(user: RevelUser) -> Client:
    token = RefreshToken.for_user(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token.access_token}"  # type: ignore[attr-defined]
    return c


def _membership_questionnaire(organization: Organization) -> Questionnaire:
    q = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    )
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])
    return q


# ---- B1: re-apply after rejection must work through the API ----


def test_apply_after_rejection_creates_fresh_row_via_api(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """A latest-row REJECTED application must not lock the user out of POST /apply.

    The preview verdict carries next_step=REAPPLY, which passes the controller's
    hard-block set; the fresh PENDING row supersedes the rejected one and (with
    no other gates configured) completes on the spot.
    """
    rejected = OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(url, data={"tier_id": str(tier.id)}, content_type="application/json")

    assert response.status_code == 201, response.content
    rejected.refresh_from_db()
    assert rejected.status == OrganizationMembershipRequest.Status.REJECTED  # old row untouched
    fresh = (
        OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user, tier=tier)
        .exclude(pk=rejected.pk)
        .get()
    )
    assert fresh.status == OrganizationMembershipRequest.Status.COMPLETED
    assert OrganizationMember.objects.filter(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).exists()


def test_join_eligibility_after_rejection_returns_reapply(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    OrganizationMembershipRequest.objects.create(
        organization=organization,
        user=nonmember_user,
        tier=tier,
        status=OrganizationMembershipRequest.Status.REJECTED,
    )
    client = _client(nonmember_user)
    url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})
    response = client.get(url, {"tier_id": str(tier.id)})
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["allowed"] is False
    assert body["next_step"] == "reapply"
    assert body["reason_code"] == "application_rejected"


# ---- B3: questionnaire_submission_id must be validated, not blindly persisted ----


def _ready_submission(user: RevelUser, questionnaire: Questionnaire) -> QuestionnaireSubmission:
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )
    return submission


def test_apply_with_other_users_submission_is_rejected(
    nonmember_user: RevelUser, member_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = _membership_questionnaire(organization)
    other_submission = _ready_submission(member_user, q)

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(other_submission.id)},
        content_type="application/json",
    )

    assert response.status_code == 400, response.content
    assert not OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()


def test_apply_with_nonexistent_submission_matches_foreign_submission_response(
    nonmember_user: RevelUser, member_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    """Nonexistent and someone-else's submission ids must fail identically (no oracle)."""
    q = _membership_questionnaire(organization)
    other_submission = _ready_submission(member_user, q)

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response_foreign = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(other_submission.id)},
        content_type="application/json",
    )
    response_missing = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(uuid.uuid4())},
        content_type="application/json",
    )

    assert response_foreign.status_code == response_missing.status_code == 400
    assert response_foreign.json() == response_missing.json()


def test_apply_with_submission_for_wrong_questionnaire_is_rejected(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    _membership_questionnaire(organization)
    unrelated_q = Questionnaire.objects.create(name="Other", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    submission = _ready_submission(nonmember_user, unrelated_q)

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(submission.id)},
        content_type="application/json",
    )

    assert response.status_code == 400, response.content


def test_apply_with_draft_submission_is_rejected(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = _membership_questionnaire(organization)
    submission = QuestionnaireSubmission.objects.create(
        user=nonmember_user,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
    )

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(submission.id)},
        content_type="application/json",
    )

    assert response.status_code == 400, response.content


def test_apply_with_submission_when_no_questionnaire_configured_is_rejected(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    unrelated_q = Questionnaire.objects.create(name="Other", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    submission = _ready_submission(nonmember_user, unrelated_q)

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(submission.id)},
        content_type="application/json",
    )

    assert response.status_code == 400, response.content


def test_apply_with_valid_own_submission_is_persisted(
    nonmember_user: RevelUser, organization: Organization, tier: MembershipTier
) -> None:
    q = _membership_questionnaire(organization)
    submission = _ready_submission(nonmember_user, q)

    client = _client(nonmember_user)
    url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
    response = client.post(
        url,
        data={"tier_id": str(tier.id), "questionnaire_submission_id": str(submission.id)},
        content_type="application/json",
    )

    assert response.status_code == 201, response.content
    application = OrganizationMembershipRequest.objects.get(organization=organization, user=nonmember_user, tier=tier)
    assert application.questionnaire_submission_id == submission.id
