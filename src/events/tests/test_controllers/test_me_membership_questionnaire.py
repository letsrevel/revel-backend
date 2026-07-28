"""Tests for /me/organizations/{slug}/membership-questionnaire/{questionnaire_id}[/submit]."""

import typing as t
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationQuestionnaire
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db

EVAL_TASK = "events.service.membership_questionnaire_service.evaluate_questionnaire_submission.delay"


@pytest.fixture(autouse=True)
def open_org(organization: Organization) -> None:
    """Make the org publicly visible so ``Organization.for_user`` returns it."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.accept_membership_requests = True
    organization.save(update_fields=["visibility", "accept_membership_requests"])


def _questionnaire(name: str = "Membership Q") -> Questionnaire:
    """A published questionnaire with one mandatory MCQ and one optional FTQ."""
    q = Questionnaire.objects.create(name=name, status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Mandatory MCQ", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="Correct", is_correct=True)
    FreeTextQuestion.objects.create(questionnaire=q, question="Optional FTQ", is_mandatory=False)
    return q


def _wrap(
    organization: Organization,
    questionnaire: Questionnaire,
    **kwargs: t.Any,
) -> OrganizationQuestionnaire:
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        **kwargs,
    )


@pytest.fixture
def membership_questionnaire(organization: Organization) -> Questionnaire:
    """The org's default membership questionnaire."""
    q = _questionnaire()
    organization.default_membership_questionnaire = _wrap(organization, q)
    organization.save(update_fields=["default_membership_questionnaire"])
    return q


def _get_url(organization: Organization, questionnaire_id: uuid.UUID) -> str:
    return reverse(
        "api:get_membership_questionnaire",
        kwargs={"slug": organization.slug, "questionnaire_id": questionnaire_id},
    )


def _submit_url(organization: Organization, questionnaire_id: uuid.UUID) -> str:
    return reverse(
        "api:submit_membership_questionnaire",
        kwargs={"slug": organization.slug, "questionnaire_id": questionnaire_id},
    )


def _payload(questionnaire: Questionnaire, status: str = "ready", answer: bool = True) -> bytes:
    body: dict[str, t.Any] = {"questionnaire_id": str(questionnaire.pk), "status": status}
    if answer:
        mcq = questionnaire.multiplechoicequestion_questions.first()
        assert mcq is not None
        option = mcq.options.first()
        assert option is not None
        body["multiple_choice_answers"] = [{"question_id": str(mcq.id), "options_id": [str(option.id)]}]
    return orjson.dumps(body)


def _ready_submission(
    user: RevelUser,
    questionnaire: Questionnaire,
    *,
    submitted_at: t.Any = None,
) -> QuestionnaireSubmission:
    return QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=submitted_at or timezone.now(),
    )


# --- GET ---------------------------------------------------------------------


def test_get_membership_questionnaire_success(
    nonmember_client: Client, organization: Organization, membership_questionnaire: Questionnaire
) -> None:
    """The org default membership questionnaire is fetchable by any user who can see the org."""
    response = nonmember_client.get(_get_url(organization, membership_questionnaire.pk))
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["id"] == str(membership_questionnaire.pk)
    assert len(data["multiple_choice_questions"]) == 1
    assert len(data["free_text_questions"]) == 1


def test_get_membership_questionnaire_via_tier_override(nonmember_client: Client, organization: Organization) -> None:
    """A tier-level override is reachable even when the org has no default."""
    q = _questionnaire("Tier Q")
    tier = MembershipTier.objects.create(
        organization=organization, name="Student", membership_questionnaire=_wrap(organization, q)
    )
    assert tier.membership_questionnaire_id is not None
    assert organization.default_membership_questionnaire_id is None

    response = nonmember_client.get(_get_url(organization, q.pk))
    assert response.status_code == 200, response.content
    assert response.json()["id"] == str(q.pk)


def test_get_unlinked_org_questionnaire_returns_404(nonmember_client: Client, organization: Organization) -> None:
    """A MEMBERSHIP questionnaire of the org that nothing points at is not reachable."""
    q = _questionnaire("Orphan")
    _wrap(organization, q)

    response = nonmember_client.get(_get_url(organization, q.pk))
    assert response.status_code == 404


def test_get_admission_questionnaire_returns_404(
    nonmember_client: Client, organization: Organization, membership_questionnaire: Questionnaire
) -> None:
    """An unrelated (admission) questionnaire of the same org is not leaked here."""
    other = _questionnaire("Admission")
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=other)

    response = nonmember_client.get(_get_url(organization, other.pk))
    assert response.status_code == 404


def test_get_foreign_org_questionnaire_returns_404(
    nonmember_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Another org's default membership questionnaire is not reachable via this org's slug."""
    other_org = Organization.objects.create(
        name="Other",
        slug="other",
        owner=organization_owner_user,
        visibility=Organization.Visibility.PUBLIC,
    )
    q = _questionnaire("Foreign")
    other_org.default_membership_questionnaire = _wrap(other_org, q)
    other_org.save(update_fields=["default_membership_questionnaire"])

    response = nonmember_client.get(_get_url(organization, q.pk))
    assert response.status_code == 404


def test_get_nonexistent_questionnaire_returns_404(nonmember_client: Client, organization: Organization) -> None:
    """An unknown questionnaire id is a 404."""
    response = nonmember_client.get(_get_url(organization, uuid.uuid4()))
    assert response.status_code == 404


def test_get_invisible_org_returns_404(
    nonmember_client: Client, organization: Organization, membership_questionnaire: Questionnaire
) -> None:
    """A private org is a 404, mirroring join-eligibility's anti-enumeration posture."""
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save(update_fields=["visibility"])

    response = nonmember_client.get(_get_url(organization, membership_questionnaire.pk))
    assert response.status_code == 404


def test_get_anonymous_returns_401(
    client: Client, organization: Organization, membership_questionnaire: Questionnaire
) -> None:
    """Fetching requires authentication."""
    response = client.get(_get_url(organization, membership_questionnaire.pk))
    assert response.status_code == 401


# --- POST /submit ------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@patch(EVAL_TASK)
def test_submit_success_queues_evaluation(
    mock_evaluate: MagicMock,
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """A ready submission is persisted and its evaluation queued.

    Uses ``transaction=True`` because the evaluation task is dispatched via
    ``transaction.on_commit``; in default pytest-django mode the wrapping
    transaction is rolled back and the callback never fires.
    """
    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["questionnaire_id"] == str(membership_questionnaire.pk)
    assert data["status"] == "ready"
    assert data["requires_evaluation"] is True
    assert data["submitted_at"]
    # The union's evaluation member is not used by this endpoint.
    assert "score" not in data

    assert QuestionnaireSubmission.objects.count() == 1
    submission = QuestionnaireSubmission.objects.get()
    assert submission.user_id == nonmember_user.pk
    mock_evaluate.assert_called_once_with(str(submission.pk))


@pytest.mark.django_db(transaction=True)
@patch(EVAL_TASK)
def test_submit_no_evaluation_required_does_not_queue(
    mock_evaluate: MagicMock,
    nonmember_client: Client,
    organization: Organization,
) -> None:
    """``requires_evaluation=False`` submissions are stored without triggering the LLM.

    Uses ``transaction=True`` — the mirror image of the test above — so that
    ``assert_not_called`` stays meaningful: the dispatch is an ``on_commit``
    callback, which in default pytest-django mode never fires, and the assertion
    would pass even if the ``requires_evaluation`` gate were removed.
    """
    q = _questionnaire("No eval")
    organization.default_membership_questionnaire = _wrap(organization, q, requires_evaluation=False)
    organization.save(update_fields=["default_membership_questionnaire"])

    response = nonmember_client.post(_submit_url(organization, q.pk), data=_payload(q), content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["requires_evaluation"] is False
    mock_evaluate.assert_not_called()


def test_submit_missing_mandatory_answer_returns_400(
    nonmember_client: Client, organization: Organization, membership_questionnaire: Questionnaire
) -> None:
    """A ready submission without the mandatory answer is rejected and persists nothing."""
    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire, answer=False),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "mandatory" in response.json()["detail"].lower()
    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_unlinked_questionnaire_returns_404(nonmember_client: Client, organization: Organization) -> None:
    """The submit guard is the same as the fetch guard."""
    q = _questionnaire("Orphan")
    _wrap(organization, q)

    response = nonmember_client.post(_submit_url(organization, q.pk), data=_payload(q), content_type="application/json")
    assert response.status_code == 404
    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_anonymous_returns_401(
    client: Client, organization: Organization, membership_questionnaire: Questionnaire
) -> None:
    """Submitting requires authentication."""
    response = client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 401
    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_while_evaluation_pending_returns_400(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """A submission awaiting evaluation blocks a second attempt."""
    _ready_submission(nonmember_user, membership_questionnaire)

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "pending evaluation" in response.json()["detail"]
    assert QuestionnaireSubmission.objects.count() == 1


def test_submit_when_already_approved_returns_400(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """An approved (still fresh) submission blocks another attempt."""
    submission = _ready_submission(nonmember_user, membership_questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "already been approved" in response.json()["detail"]


@patch(EVAL_TASK)
def test_submit_when_approval_is_stale_is_allowed(
    mock_evaluate: MagicMock,
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
) -> None:
    """An approval older than ``max_submission_age`` re-opens submission (the gate asks again)."""
    q = _questionnaire("Ages out")
    oq = _wrap(organization, q, max_submission_age=timedelta(days=1))
    organization.default_membership_questionnaire = oq
    organization.save(update_fields=["default_membership_questionnaire"])

    submission = _ready_submission(nonmember_user, q, submitted_at=timezone.now() - timedelta(days=30))
    evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )
    QuestionnaireEvaluation.objects.filter(pk=evaluation.pk).update(updated_at=timezone.now() - timedelta(days=30))

    response = nonmember_client.post(_submit_url(organization, q.pk), data=_payload(q), content_type="application/json")
    assert response.status_code == 200, response.content
    assert QuestionnaireSubmission.objects.filter(user=nonmember_user, questionnaire=q).count() == 2


def test_submit_second_time_without_evaluation_returns_400(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
) -> None:
    """With ``requires_evaluation=False`` one ready submission is final."""
    q = _questionnaire("No eval")
    organization.default_membership_questionnaire = _wrap(organization, q, requires_evaluation=False)
    organization.save(update_fields=["default_membership_questionnaire"])
    _ready_submission(nonmember_user, q)

    response = nonmember_client.post(_submit_url(organization, q.pk), data=_payload(q), content_type="application/json")
    assert response.status_code == 400
    assert "already submitted" in response.json()["detail"]


def test_submit_after_rejection_without_retake_returns_400(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """``can_retake_after=None`` matches the gate's terminal failure verdict."""
    assert membership_questionnaire.can_retake_after is None
    submission = _ready_submission(nonmember_user, membership_questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "cannot be retaken" in response.json()["detail"]


def test_submit_after_rejection_during_cooldown_returns_400(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """A rejected attempt inside the cooldown window is refused."""
    membership_questionnaire.can_retake_after = timedelta(days=7)
    membership_questionnaire.save(update_fields=["can_retake_after"])
    submission = _ready_submission(nonmember_user, membership_questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "You can retry after" in response.json()["detail"]


@patch(EVAL_TASK)
def test_submit_after_rejection_once_cooldown_elapsed(
    mock_evaluate: MagicMock,
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """Once the cooldown has elapsed a retake is accepted."""
    membership_questionnaire.can_retake_after = timedelta(hours=1)
    membership_questionnaire.save(update_fields=["can_retake_after"])
    submission = _ready_submission(
        nonmember_user, membership_questionnaire, submitted_at=timezone.now() - timedelta(days=1)
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert QuestionnaireSubmission.objects.filter(user=nonmember_user).count() == 2


def test_submit_after_rejection_with_attempts_exhausted_returns_400(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """``max_attempts`` caps retakes even when the cooldown has elapsed."""
    membership_questionnaire.can_retake_after = timedelta(hours=1)
    membership_questionnaire.max_attempts = 1
    membership_questionnaire.save(update_fields=["can_retake_after", "max_attempts"])
    submission = _ready_submission(
        nonmember_user, membership_questionnaire, submitted_at=timezone.now() - timedelta(days=1)
    )
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "maximum number of attempts" in response.json()["detail"]
    assert QuestionnaireSubmission.objects.filter(user=nonmember_user).count() == 1


def test_submit_unblocks_join_eligibility(nonmember_client: Client, organization: Organization) -> None:
    """The whole point of #783: submitting clears the SUBMIT_QUESTIONNAIRE dead end."""
    q = _questionnaire("Gatekeeper")
    organization.default_membership_questionnaire = _wrap(organization, q, requires_evaluation=False)
    organization.save(update_fields=["default_membership_questionnaire"])
    eligibility_url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})

    before = nonmember_client.get(eligibility_url)
    assert before.status_code == 200
    assert before.json()["next_step"] == "submit_questionnaire"
    assert before.json()["questionnaire_id"] == str(q.pk)

    submitted = nonmember_client.post(
        _submit_url(organization, q.pk), data=_payload(q), content_type="application/json"
    )
    assert submitted.status_code == 200, submitted.content

    after = nonmember_client.get(eligibility_url)
    assert after.status_code == 200
    assert after.json()["allowed"] is True


@patch(EVAL_TASK)
def test_submit_draft_skips_retake_validation(
    mock_evaluate: MagicMock,
    nonmember_client: Client,
    nonmember_user: RevelUser,
    organization: Organization,
    membership_questionnaire: Questionnaire,
) -> None:
    """A draft save is never gated by the retake policy and never queues evaluation."""
    _ready_submission(nonmember_user, membership_questionnaire)

    response = nonmember_client.post(
        _submit_url(organization, membership_questionnaire.pk),
        data=_payload(membership_questionnaire, status="draft"),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "draft"
    mock_evaluate.assert_not_called()
