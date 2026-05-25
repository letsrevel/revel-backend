"""Signal-based guards on questionnaire mutations for polls."""

import pytest
from django.utils import timezone

from events.models.organization import Organization
from polls.exceptions import PollQuestionLockedError
from polls.models import Poll
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)

pytestmark = pytest.mark.django_db


def _open_poll(organization: Organization) -> Poll:
    q = Questionnaire.objects.create(name="locked")
    return Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )


def test_mc_question_create_blocked_when_poll_open(organization: Organization) -> None:
    poll = _open_poll(organization)
    with pytest.raises(PollQuestionLockedError):
        MultipleChoiceQuestion.objects.create(questionnaire=poll.questionnaire, question="new?")


def test_mc_question_create_allowed_when_poll_draft(organization: Organization) -> None:
    q = Questionnaire.objects.create(name="draft")
    Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.DRAFT,
    )
    # Should not raise.
    MultipleChoiceQuestion.objects.create(questionnaire=q, question="ok?")


def test_mc_question_create_allowed_when_no_poll(organization: Organization) -> None:
    q = Questionnaire.objects.create(name="lone")
    MultipleChoiceQuestion.objects.create(questionnaire=q, question="ok?")  # not raising


def test_mc_option_delete_blocked_when_poll_open(organization: Organization) -> None:
    q = Questionnaire.objects.create(name="locked")
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="x")
    opt = MultipleChoiceOption.objects.create(question=mcq, option="a")
    Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )
    with pytest.raises(PollQuestionLockedError):
        opt.delete()


def test_section_create_blocked_when_poll_closed(organization: Organization) -> None:
    q = Questionnaire.objects.create(name="locked")
    Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.CLOSED,
        opened_at=timezone.now(),
        closed_at=timezone.now(),
    )
    with pytest.raises(PollQuestionLockedError):
        QuestionnaireSection.objects.create(questionnaire=q, name="s")
