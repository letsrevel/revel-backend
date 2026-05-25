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
    # Build questions BEFORE the poll so the question-lockdown signal allows mutations.
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Pick one")
    options = [MultipleChoiceOption.objects.create(question=mcq, option=f"opt-{i}") for i in range(3)]
    ftq = FreeTextQuestion.objects.create(questionnaire=q, question="Anything?")
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.OPEN,
    )
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


def test_mc_voters_hidden_when_viewer_anonymous(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    """``voters`` is None (not []) on every option when identity is hidden (#450)."""
    poll, options, _ = poll_with_questions
    u = revel_user_factory()
    sub = QuestionnaireSubmission.objects.create(
        user=u,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    MultipleChoiceAnswer.objects.create(submission=sub, question=options[0].question, option=options[0])

    result = compute_poll_results(poll, viewer_sees_identity=False)
    stat = result.mc_question_stats[0]
    assert all(o.voters is None for o in stat.options)


def test_mc_voters_exposed_when_viewer_sees_identity(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    """When identity is visible, picked options list voters and unpicked ones get [] (#450)."""
    poll, options, _ = poll_with_questions
    voter = revel_user_factory(preferred_name="Diana D.")
    sub = QuestionnaireSubmission.objects.create(
        user=voter,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    MultipleChoiceAnswer.objects.create(submission=sub, question=options[0].question, option=options[0])

    result = compute_poll_results(poll, viewer_sees_identity=True)
    by_text = {o.option_text: o for o in result.mc_question_stats[0].options}

    picked = by_text["opt-0"]
    assert picked.voters is not None
    assert len(picked.voters) == 1
    assert picked.voters[0].user_id == voter.id
    assert picked.voters[0].user_display_name == "Diana D."
    assert picked.voters[0].user_email == voter.email

    # Unpicked options: identity visible, so [] (someone could see who, nobody did) — not None.
    assert by_text["opt-1"].voters == []
    assert by_text["opt-2"].voters == []


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
    u = revel_user_factory(preferred_name="Karen K.")
    sub = QuestionnaireSubmission.objects.create(
        user=u,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    FreeTextAnswer.objects.create(submission=sub, question=ftq, answer="hi")

    result = compute_poll_results(poll, viewer_sees_identity=True)
    entry = result.free_text_responses[0]
    assert entry.user_id == u.id
    assert entry.user_display_name == "Karen K."
    assert entry.user_email == u.email


def test_free_text_responses_omit_display_fields_when_anonymous(
    poll_with_questions: tuple[Poll, list[t.Any], list[t.Any]],
    revel_user_factory: t.Any,
) -> None:
    """``user_display_name`` / ``user_email`` are gated on viewer_sees_identity.

    Regression for #448: when staff_anonymous is True (or the viewer is a
    non-staff voter on a public_anonymous poll), the response must not leak
    the voter's display name or email through these new fields either.
    """
    poll, _, free_text_qs = poll_with_questions
    ftq = free_text_qs[0]
    u = revel_user_factory(preferred_name="Karen K.")
    sub = QuestionnaireSubmission.objects.create(
        user=u,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    FreeTextAnswer.objects.create(submission=sub, question=ftq, answer="hi")

    result = compute_poll_results(poll, viewer_sees_identity=False)
    entry = result.free_text_responses[0]
    assert entry.user_id is None
    assert entry.user_display_name is None
    assert entry.user_email is None
