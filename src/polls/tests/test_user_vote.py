"""Tests for polls.service.user_vote.build_user_vote (issue #449)."""

import typing as t

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from polls.models import Poll
from polls.service.user_vote import build_user_vote
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


class PollSetup(t.NamedTuple):
    """A poll plus the questions on its (locked) questionnaire."""

    poll: Poll
    mcq: MultipleChoiceQuestion
    mc_options: list[MultipleChoiceOption]
    ftq: FreeTextQuestion
    fuq: FileUploadQuestion


@pytest.fixture
def poll_setup(organization: t.Any) -> PollSetup:
    """A public OPEN poll whose questionnaire has one multi-select MC, one
    free-text, and one file-upload question.

    All questions are created BEFORE the poll: the ``polls.signals`` lockdown
    forbids structural mutations to a poll's questionnaire once the poll exists
    (it is created OPEN here), so question creation must precede the poll.
    """
    q = Questionnaire.objects.create(name="vote-readback Q")
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Pick some", allow_multiple_answers=True)
    mc_options = [MultipleChoiceOption.objects.create(question=mcq, option=f"opt-{i}") for i in range(3)]
    ftq = FreeTextQuestion.objects.create(questionnaire=q, question="Why?")
    fuq = FileUploadQuestion.objects.create(questionnaire=q, question="Upload", max_files=2)
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility="public",
        status=Poll.PollStatus.OPEN,
    )
    return PollSetup(poll=poll, mcq=mcq, mc_options=mc_options, ftq=ftq, fuq=fuq)


def _ready_submission(poll: Poll, user: t.Any) -> QuestionnaireSubmission:
    return QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )


def test_returns_none_for_anonymous_user(poll_setup: PollSetup) -> None:
    assert build_user_vote(AnonymousUser(), poll_setup.poll) is None


def test_returns_none_when_user_has_not_voted(poll_setup: PollSetup, revel_user_factory: t.Any) -> None:
    assert build_user_vote(revel_user_factory(), poll_setup.poll) is None


def test_returns_none_for_draft_submission(poll_setup: PollSetup, revel_user_factory: t.Any) -> None:
    """A DRAFT (un-submitted) submission is not a cast vote and must not pre-fill."""
    user = revel_user_factory()
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=poll_setup.poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
    )
    assert build_user_vote(user, poll_setup.poll) is None


def test_groups_multiple_choice_options_by_question(poll_setup: PollSetup, revel_user_factory: t.Any) -> None:
    """Two selected options on one multi-select question collapse to one entry."""
    poll, mcq, opts = poll_setup.poll, poll_setup.mcq, poll_setup.mc_options
    user = revel_user_factory()
    sub = _ready_submission(poll, user)
    MultipleChoiceAnswer.objects.create(submission=sub, question=mcq, option=opts[0])
    MultipleChoiceAnswer.objects.create(submission=sub, question=mcq, option=opts[2])

    vote = build_user_vote(user, poll)

    assert vote is not None
    assert len(vote.mc_answers) == 1
    entry = vote.mc_answers[0]
    assert entry.question_id == mcq.id
    assert set(entry.option_ids) == {opts[0].id, opts[2].id}
    assert vote.free_text_answers == []
    assert vote.file_upload_answers == []


def test_reads_free_text_answer(poll_setup: PollSetup, revel_user_factory: t.Any) -> None:
    poll, ftq = poll_setup.poll, poll_setup.ftq
    user = revel_user_factory()
    sub = _ready_submission(poll, user)
    FreeTextAnswer.objects.create(submission=sub, question=ftq, answer="because")

    vote = build_user_vote(user, poll)

    assert vote is not None
    assert len(vote.free_text_answers) == 1
    assert vote.free_text_answers[0].question_id == ftq.id
    assert vote.free_text_answers[0].answer == "because"


def test_reads_file_upload_answer(poll_setup: PollSetup, revel_user_factory: t.Any) -> None:
    poll, fuq = poll_setup.poll, poll_setup.fuq
    user = revel_user_factory()
    qfile = QuestionnaireFile.objects.create(
        uploader=user,
        file=SimpleUploadedFile("doc.pdf", b"data", content_type="application/pdf"),
        original_filename="doc.pdf",
        file_hash="user-vote-hash-001",
        mime_type="application/pdf",
        file_size=4,
    )
    sub = _ready_submission(poll, user)
    answer = FileUploadAnswer.objects.create(submission=sub, question=fuq)
    answer.files.set([qfile])

    vote = build_user_vote(user, poll)

    assert vote is not None
    assert len(vote.file_upload_answers) == 1
    assert vote.file_upload_answers[0].question_id == fuq.id
    assert vote.file_upload_answers[0].file_ids == [qfile.id]
