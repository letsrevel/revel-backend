"""Tests for polls.service.aggregation."""

import typing as t

import pytest
from django.utils import timezone

from polls.models import Poll
from polls.service.aggregation import compute_poll_results
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def poll_with_questions(organization: t.Any, revel_user_factory: t.Any) -> tuple[Poll, list[t.Any], list[t.Any]]:
    """A poll with one MC question (3 options) and one free-text question."""
    q = Questionnaire.objects.create(name="poll Q")
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.OPEN,
    )
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Pick one")
    options = [MultipleChoiceOption.objects.create(question=mcq, option=f"opt-{i}") for i in range(3)]
    ftq = FreeTextQuestion.objects.create(questionnaire=q, question="Anything?")
    return poll, options, [ftq]


def test_total_voters_counts_distinct_users(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    poll, options, _ = poll_with_questions
    u1 = revel_user_factory()
    u2 = revel_user_factory()
    for u in (u1, u2):
        sub = QuestionnaireSubmission.objects.create(
            user=u,
            questionnaire=poll.questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        MultipleChoiceAnswer.objects.create(submission=sub, question=options[0].question, option=options[0])

    result = compute_poll_results(poll, viewer_sees_identity=False)
    assert result.total_voters == 2


def test_mc_distribution_uses_existing_aggregation(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    poll, options, _ = poll_with_questions
    for i, opt in enumerate(options):
        u = revel_user_factory()
        sub = QuestionnaireSubmission.objects.create(
            user=u,
            questionnaire=poll.questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        # opt[0] receives 2 votes, opt[1] receives 1, opt[2] receives 0
        target = options[0] if i in (0, 1) else options[1] if i == 2 else options[2]
        MultipleChoiceAnswer.objects.create(submission=sub, question=opt.question, option=target)

    result = compute_poll_results(poll, viewer_sees_identity=False)
    assert len(result.mc_question_stats) == 1
    stat = result.mc_question_stats[0]
    counts = {o.option_text: o.count for o in stat.options}
    assert counts["opt-0"] == 2
    assert counts["opt-1"] == 1
    assert counts["opt-2"] == 0


def test_free_text_responses_hide_identity_when_viewer_anonymous(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    poll, _, free_text_qs = poll_with_questions
    ftq = free_text_qs[0]
    u = revel_user_factory()
    sub = QuestionnaireSubmission.objects.create(
        user=u,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    FreeTextAnswer.objects.create(submission=sub, question=ftq, answer="hi")

    result = compute_poll_results(poll, viewer_sees_identity=False)
    assert len(result.free_text_responses) == 1
    assert result.free_text_responses[0].answer == "hi"
    assert result.free_text_responses[0].user_id is None


def test_free_text_responses_show_identity_when_viewer_sees_identity(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    poll, _, free_text_qs = poll_with_questions
    ftq = free_text_qs[0]
    u = revel_user_factory()
    sub = QuestionnaireSubmission.objects.create(
        user=u,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    FreeTextAnswer.objects.create(submission=sub, question=ftq, answer="hi")

    result = compute_poll_results(poll, viewer_sees_identity=True)
    assert result.free_text_responses[0].user_id == u.id
