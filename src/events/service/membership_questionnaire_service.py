"""Member-facing membership-questionnaire support.

The org-scoped twin of :mod:`events.service.event_questionnaire_service`: it
resolves which questionnaire a user is allowed to fill for a *membership*
application and performs the submission. There is no event to hang the
submission off, so no ``EventQuestionnaireSubmission`` tracking row is written —
:class:`events.service.membership_manager.gates.MembershipQuestionnaireGate`
reads the plain ``QuestionnaireSubmission`` rows for (user, questionnaire).
"""

from __future__ import annotations

import typing as t
from uuid import UUID

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationQuestionnaire
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission
from questionnaires.schema import QuestionnaireSubmissionSchema
from questionnaires.service.submission_service import SubmissionService
from questionnaires.tasks import evaluate_questionnaire_submission

if t.TYPE_CHECKING:
    from datetime import datetime


def get_membership_org_questionnaire(
    organization: Organization,
    questionnaire_id: UUID,
) -> OrganizationQuestionnaire:
    """Validate that a questionnaire is reachable as a membership questionnaire of an organization.

    The org-scoped analogue of
    ``EventPublicBaseController.get_org_questionnaire_for_event``. A questionnaire
    is reachable when its ``OrganizationQuestionnaire`` wrapper belongs to
    *organization* **and** is referenced either by
    ``Organization.default_membership_questionnaire`` or by some tier's
    ``MembershipTier.membership_questionnaire`` override. Resolving the tier the
    caller would actually apply at is deliberately skipped: the FE passes back
    the exact ``questionnaire_id`` the eligibility pipeline handed it, and this
    check is narrow enough not to leak the org's unrelated questionnaires
    (admission/feedback/generic wrappers all fail it).

    Args:
        organization: The organization whose membership questionnaire is requested.
        questionnaire_id: The ``Questionnaire`` primary key (not the wrapper's).

    Returns:
        The ``OrganizationQuestionnaire`` wrapper, with ``questionnaire`` selected.

    Raises:
        Http404: If the questionnaire does not gate membership in this organization.
    """
    reachable_ids: set[UUID] = set(
        MembershipTier.objects.filter(organization=organization)
        .exclude(membership_questionnaire=None)
        .values_list("membership_questionnaire_id", flat=True)
    )
    if organization.default_membership_questionnaire_id:
        reachable_ids.add(organization.default_membership_questionnaire_id)

    org_questionnaire = (
        OrganizationQuestionnaire.objects.filter(
            organization=organization,
            questionnaire_id=questionnaire_id,
            pk__in=reachable_ids,
        )
        .select_related("questionnaire")
        .first()
    )
    if org_questionnaire is None:
        raise Http404(_("Questionnaire not found for this organization."))
    return org_questionnaire


def _validate_resubmission(*, user: RevelUser, org_questionnaire: OrganizationQuestionnaire) -> None:
    """Validate that the user may (re)submit this membership questionnaire.

    Mirrors ``_validate_admission_resubmission`` in
    :mod:`events.service.event_questionnaire_service`, but counts attempts over
    the user's ``QuestionnaireSubmission`` rows (membership submissions are not
    event-scoped) and stays in lockstep with ``MembershipQuestionnaireGate`` so
    the endpoint never contradicts the ``next_step`` the FE was given.

    Raises:
        HttpError: 400 when a submission is pending, already approved and still
            fresh, or the retake policy forbids another attempt.
    """
    questionnaire = org_questionnaire.questionnaire
    submissions = list(
        QuestionnaireSubmission.objects.filter(
            user=user,
            questionnaire=questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        .select_related("evaluation")
        .order_by("-submitted_at")
    )
    if not submissions:
        return

    # No evaluation required → the first READY submission already satisfies the gate.
    if not org_questionnaire.requires_evaluation:
        raise HttpError(400, str(_("You have already submitted this questionnaire.")))

    latest = submissions[0]
    evaluation: QuestionnaireEvaluation | None = getattr(latest, "evaluation", None)
    statuses = QuestionnaireEvaluation.QuestionnaireEvaluationStatus

    if evaluation is None or evaluation.status == statuses.PENDING_REVIEW:
        raise HttpError(400, str(_("You have a submission pending evaluation.")))

    if evaluation.status == statuses.APPROVED:
        if _approval_is_stale(org_questionnaire, evaluation):
            # max_submission_age elapsed: the gate asks for a fresh submission again.
            return
        raise HttpError(400, str(_("Your questionnaire has already been approved.")))

    # Rejected: apply the retake policy.
    # MembershipQuestionnaireGate blocks the cap ahead of the retake policy with
    # MEMBERSHIP_QUESTIONNAIRE_ATTEMPTS_EXHAUSTED, so a user at the cap is never told to
    # submit. Enforcing it here too is still required — the gate is advisory, and without
    # this an AUTOMATIC-mode membership questionnaire could be brute-forced.
    if 0 < questionnaire.max_attempts <= len(submissions):
        raise HttpError(400, str(_("You have reached the maximum number of attempts.")))

    if questionnaire.can_retake_after is None:
        # Matches the gate's terminal MEMBERSHIP_QUESTIONNAIRE_FAILED verdict.
        raise HttpError(400, str(_("This questionnaire cannot be retaken.")))

    # submitted_at is guaranteed to be set for READY submissions (see QuestionnaireSubmission.save())
    retry_on = t.cast("datetime", latest.submitted_at) + questionnaire.can_retake_after
    if retry_on > timezone.now():
        raise HttpError(400, str(_("You can retry after %(retry_on)s.") % {"retry_on": retry_on}))


def _approval_is_stale(
    org_questionnaire: OrganizationQuestionnaire,
    evaluation: QuestionnaireEvaluation,
) -> bool:
    """Whether an APPROVED evaluation has aged out of ``max_submission_age``."""
    if not org_questionnaire.max_submission_age:
        return False
    return bool((evaluation.updated_at + org_questionnaire.max_submission_age) < timezone.now())


@transaction.atomic
def submit_membership_questionnaire(
    *,
    user: RevelUser,
    org_questionnaire: OrganizationQuestionnaire,
    questionnaire_service: SubmissionService,
    submission_schema: QuestionnaireSubmissionSchema,
) -> QuestionnaireSubmission:
    """Submit a membership questionnaire and queue its evaluation.

    Args:
        user: The submitting user.
        org_questionnaire: The wrapper resolved by :func:`get_membership_org_questionnaire`.
        questionnaire_service: The ``SubmissionService`` for the questionnaire.
        submission_schema: The submission payload.

    Returns:
        The created ``QuestionnaireSubmission``.

    Raises:
        HttpError: If the retake policy forbids this submission.
    """
    if submission_schema.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY:
        _validate_resubmission(user=user, org_questionnaire=org_questionnaire)

    db_submission = questionnaire_service.submit(user, submission_schema)

    if (
        submission_schema.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
        and org_questionnaire.requires_evaluation
        and questionnaire_service.questionnaire.evaluation_mode
        in (
            Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
            Questionnaire.QuestionnaireEvaluationMode.HYBRID,
        )
    ):
        transaction.on_commit(lambda: evaluate_questionnaire_submission.delay(str(db_submission.pk)))

    return db_submission
