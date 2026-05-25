"""Aggregate poll results for the API response.

Reuses :func:`events.service.event_questionnaire_service.aggregate_mc_distributions`
to compute the multiple-choice statistics and adds poll-specific free-text
aggregation that honours the anonymity contract (see :class:`polls.models.Poll`).
"""

from collections import defaultdict
from uuid import UUID

from django.db.models import QuerySet

from events.service.event_questionnaire_service import aggregate_mc_distributions
from polls.models import Poll
from polls.schema import (
    PollFreeTextResponseSchema,
    PollMcOptionStatSchema,
    PollMcQuestionStatSchema,
    PollResultsSchema,
    PollVoterSchema,
)
from questionnaires.models import FreeTextAnswer, MultipleChoiceAnswer, QuestionnaireSubmission


def compute_poll_results(poll: Poll, *, viewer_sees_identity: bool) -> PollResultsSchema:
    """Compute the result payload for a poll.

    Args:
        poll: the poll to aggregate.
        viewer_sees_identity: whether to attach voter identity to free-text
            entries AND to each MC option's ``voters`` list. Controllers pass
            ``True`` only when the viewer is staff AND ``poll.staff_anonymous=False``,
            OR the viewer is non-staff AND ``poll.public_anonymous=False``.
            Otherwise pass ``False`` (free-text identity fields stay ``None``
            and MC ``voters`` stays ``None``).

    Returns:
        A :class:`PollResultsSchema` with the total voter count,
        multiple-choice distributions, and free-text responses.
    """
    base_qs = QuestionnaireSubmission.objects.filter(
        questionnaire_id=poll.questionnaire_id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at__isnull=False,
    )
    total_voters = base_qs.values("user_id").distinct().count()
    mc_stats = _build_mc_question_stats(poll, base_qs, viewer_sees_identity=viewer_sees_identity)

    # ``submission__user`` is select_related only when the viewer is allowed
    # to see identity, so we don't pay for the join in the anonymous path.
    ft_answers = FreeTextAnswer.objects.filter(submission__in=base_qs).order_by("submission__submitted_at", "id")
    if viewer_sees_identity:
        ft_answers = ft_answers.select_related("submission__user")
    else:
        ft_answers = ft_answers.select_related("submission")

    free_text_responses: list[PollFreeTextResponseSchema] = []
    for ans in ft_answers:
        submitted_at = ans.submission.submitted_at
        if submitted_at is None:  # pragma: no cover - filtered out in base_qs above
            continue
        if viewer_sees_identity:
            voter = ans.submission.user
            free_text_responses.append(
                PollFreeTextResponseSchema(
                    question_id=ans.question_id,
                    answer=ans.answer,
                    answered_at=submitted_at,
                    user_id=voter.id,
                    user_display_name=voter.get_display_name(),
                    user_email=voter.email,
                )
            )
        else:
            free_text_responses.append(
                PollFreeTextResponseSchema(
                    question_id=ans.question_id,
                    answer=ans.answer,
                    answered_at=submitted_at,
                )
            )

    return PollResultsSchema(
        total_voters=total_voters,
        mc_question_stats=mc_stats,
        free_text_responses=free_text_responses,
    )


def _build_mc_question_stats(
    poll: Poll,
    base_qs: QuerySet[QuestionnaireSubmission],
    *,
    viewer_sees_identity: bool,
) -> list[PollMcQuestionStatSchema]:
    """Build the poll MC stats, attaching per-option voters when allowed.

    The aggregate counts come from the shared
    :func:`events.service.event_questionnaire_service.aggregate_mc_distributions`
    helper; this wraps them in the poll-specific schema and, when
    ``viewer_sees_identity`` is True, attaches the voter list to each option.
    When identity is hidden, ``voters`` is ``None`` (not ``[]``) so the FE can
    tell "anonymous poll" apart from "nobody picked this option".
    """
    aggregate = aggregate_mc_distributions(poll.questionnaire_id, base_qs)
    voters_by_option = _mc_voters_by_option(poll.questionnaire_id, base_qs) if viewer_sees_identity else {}
    return [
        PollMcQuestionStatSchema(
            question_id=question.question_id,
            question_text=question.question_text,
            options=[
                PollMcOptionStatSchema(
                    option_id=option.option_id,
                    option_text=option.option_text,
                    is_correct=option.is_correct,
                    count=option.count,
                    voters=(voters_by_option.get(option.option_id, []) if viewer_sees_identity else None),
                )
                for option in question.options
            ],
        )
        for question in aggregate
    ]


def _mc_voters_by_option(
    questionnaire_id: UUID,
    base_qs: QuerySet[QuestionnaireSubmission],
) -> dict[UUID, list[PollVoterSchema]]:
    """Map each MC option id to the ordered list of voters who selected it.

    Only called on the identity-visible path, so ``submission__user`` is
    select_related here unconditionally. Ordered by submission time then answer
    id for deterministic output, matching the free-text ordering.
    """
    answers = (
        MultipleChoiceAnswer.objects.filter(
            submission__in=base_qs,
            question__questionnaire_id=questionnaire_id,
        )
        .select_related("submission__user")
        .order_by("submission__submitted_at", "id")
    )
    voters_by_option: dict[UUID, list[PollVoterSchema]] = defaultdict(list)
    for answer in answers:
        voter = answer.submission.user
        voters_by_option[answer.option_id].append(
            PollVoterSchema(
                user_id=voter.id,
                user_display_name=voter.get_display_name(),
                user_email=voter.email,
            )
        )
    return voters_by_option
