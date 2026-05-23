"""Aggregate poll results for the API response.

Reuses :func:`events.service.event_questionnaire_service.aggregate_mc_distributions`
to compute the multiple-choice statistics and adds poll-specific free-text
aggregation that honours the anonymity contract (see :class:`polls.models.Poll`).
"""

from events.service.event_questionnaire_service import aggregate_mc_distributions
from polls.models import Poll
from polls.schema import PollFreeTextResponseSchema, PollResultsSchema
from questionnaires.models import FreeTextAnswer, QuestionnaireSubmission


def compute_poll_results(poll: Poll, *, viewer_sees_identity: bool) -> PollResultsSchema:
    """Compute the result payload for a poll.

    Args:
        poll: the poll to aggregate.
        viewer_sees_identity: whether to populate ``user_id`` on free-text
            entries. Controllers pass ``True`` only when the viewer is staff
            AND ``poll.staff_anonymous=False``, OR the viewer is non-staff
            AND ``poll.public_anonymous=False``. Otherwise pass ``False``.

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
    mc_stats = aggregate_mc_distributions(poll.questionnaire_id, base_qs)

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
