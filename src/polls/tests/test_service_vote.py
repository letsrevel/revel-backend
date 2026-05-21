"""Tests for poll_service.vote and poll_service.withdraw_vote."""

import typing as t

import pytest
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from polls.exceptions import (
    PollNotEligibleError,
    PollNotOpenError,
    PollVoteAlreadyCastError,
)
from polls.models import Poll
from polls.schema import (
    McAnswerInput,
    PollVoteSchema,
)
from polls.service import poll_service
from questionnaires.models import (
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def open_poll_with_mc(
    organization: t.Any,
) -> tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]]:
    """Build an OPEN poll with a single MC question and two options."""
    q = Questionnaire.objects.create(name="Q")
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="pick")
    options = [MultipleChoiceOption.objects.create(question=mcq, option=f"o-{i}") for i in range(2)]
    return poll, mcq, options


def test_vote_creates_submission_and_answer(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    user = revel_user_factory()
    poll_service.vote(
        user=user,
        poll_id=poll.id,
        payload=PollVoteSchema(
            mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])],
        ),
    )
    sub = QuestionnaireSubmission.objects.get(user=user, questionnaire=poll.questionnaire)
    assert sub.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    assert sub.submitted_at is not None
    assert MultipleChoiceAnswer.objects.filter(submission=sub).count() == 1


def test_vote_on_closed_poll_raises(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    poll.status = Poll.PollStatus.CLOSED
    poll.closed_at = timezone.now()
    poll.save(update_fields=["status", "closed_at"])
    with pytest.raises(PollNotOpenError):
        poll_service.vote(
            user=revel_user_factory(),
            poll_id=poll.id,
            payload=PollVoteSchema(
                mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])],
            ),
        )


def test_vote_when_not_eligible_raises(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    poll.vote_visibility = ResourceVisibility.MEMBERS_ONLY
    poll.save(update_fields=["vote_visibility"])
    with pytest.raises(PollNotEligibleError):
        poll_service.vote(
            user=revel_user_factory(),
            poll_id=poll.id,
            payload=PollVoteSchema(
                mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])],
            ),
        )


def test_double_vote_blocked_when_changes_disallowed(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    user = revel_user_factory()
    payload = PollVoteSchema(mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])])
    poll_service.vote(user=user, poll_id=poll.id, payload=payload)
    with pytest.raises(PollVoteAlreadyCastError):
        poll_service.vote(user=user, poll_id=poll.id, payload=payload)


def test_vote_change_replaces_answers(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    poll.allow_vote_changes = True
    poll.save(update_fields=["allow_vote_changes"])
    user = revel_user_factory()
    poll_service.vote(
        user=user,
        poll_id=poll.id,
        payload=PollVoteSchema(mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])]),
    )
    poll_service.vote(
        user=user,
        poll_id=poll.id,
        payload=PollVoteSchema(mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[1].id])]),
    )
    sub = QuestionnaireSubmission.objects.get(user=user, questionnaire=poll.questionnaire)
    answers = list(MultipleChoiceAnswer.objects.filter(submission=sub))
    assert len(answers) == 1
    assert answers[0].option_id == options[1].id


def test_withdraw_vote_deletes_submission(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    poll.allow_vote_changes = True
    poll.save(update_fields=["allow_vote_changes"])
    user = revel_user_factory()
    poll_service.vote(
        user=user,
        poll_id=poll.id,
        payload=PollVoteSchema(mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])]),
    )
    poll_service.withdraw_vote(user=user, poll_id=poll.id)
    assert not QuestionnaireSubmission.objects.filter(user=user, questionnaire=poll.questionnaire).exists()


def test_withdraw_vote_when_changes_disallowed_raises(
    open_poll_with_mc: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
    revel_user_factory: t.Any,
) -> None:
    poll, mcq, options = open_poll_with_mc
    user = revel_user_factory()
    poll_service.vote(
        user=user,
        poll_id=poll.id,
        payload=PollVoteSchema(mc_answers=[McAnswerInput(question_id=mcq.id, option_ids=[options[0].id])]),
    )
    with pytest.raises(PollVoteAlreadyCastError):
        poll_service.withdraw_vote(user=user, poll_id=poll.id)
