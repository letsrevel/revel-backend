"""Reconstruct the requesting user's own ballot for pre-fill on "Change my vote".

A poll's votes are stored as a :class:`questionnaires.models.QuestionnaireSubmission`
plus per-question answer rows (one row per selected MC option, one per free-text
answer, one per file-upload answer). The frontend's vote form, however, speaks the
:class:`polls.schema.PollVoteSchema` shape (MC answers grouped by question with a
list of ``option_ids``). This module reshapes the stored answers back into that
payload so the form can be initialised from the user's existing vote.

This only ever exposes the caller's OWN answers to themselves; the poll anonymity
flags govern identity exposure in aggregated results, not a user's view of their
own ballot, so they're intentionally not consulted here (see issue #449).
"""

from collections import defaultdict
from uuid import UUID

from polls.models import Poll
from polls.schema import FileUploadAnswerInput, FreeTextAnswerInput, McAnswerInput, PollVoteSchema
from polls.types import UserLike
from questionnaires.models import QuestionnaireSubmission


def build_user_vote(user: UserLike, poll: Poll) -> PollVoteSchema | None:
    """Return the caller's current ballot for ``poll``, or ``None`` if they haven't voted.

    Args:
        user: The requesting user. Anonymous users always get ``None``.
        poll: The poll whose questionnaire backs the submission lookup.

    Returns:
        A :class:`PollVoteSchema` mirroring the vote request body (MC answers
        grouped by question), or ``None`` when the user is anonymous or has no
        READY submission for the poll's questionnaire.
    """
    if user.is_anonymous:
        return None

    submission = (
        QuestionnaireSubmission.objects.filter(
            user=user,
            questionnaire_id=poll.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        .prefetch_related(
            "multiplechoiceanswer_answers",
            "freetextanswer_answers",
            "fileuploadanswer_answers__files",
        )
        .first()
    )
    if submission is None:
        return None

    # One MultipleChoiceAnswer row per selected option; the form expects them
    # collapsed into a single entry per question with a list of option ids.
    mc_option_ids_by_question: dict[UUID, list[UUID]] = defaultdict(list)
    for mc in submission.multiplechoiceanswer_answers.all():
        mc_option_ids_by_question[mc.question_id].append(mc.option_id)

    return PollVoteSchema(
        mc_answers=[
            McAnswerInput(question_id=question_id, option_ids=option_ids)
            for question_id, option_ids in mc_option_ids_by_question.items()
        ],
        free_text_answers=[
            FreeTextAnswerInput(question_id=ft.question_id, answer=ft.answer)
            for ft in submission.freetextanswer_answers.all()
        ],
        file_upload_answers=[
            FileUploadAnswerInput(question_id=fu.question_id, file_ids=[f.id for f in fu.files.all()])
            for fu in submission.fileuploadanswer_answers.all()
        ],
    )
