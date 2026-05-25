"""Tests for GET /polls/{id}/results."""

import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import (
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


def test_results_denied_when_timing_never(
    authenticated_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=Poll.PollResultTiming.NEVER,
        status=Poll.PollStatus.CLOSED,
        closed_at=timezone.now(),
    )
    response = authenticated_client.get(f"/api/polls/{poll.id}/results")
    assert response.status_code == 403


def test_results_visible_after_close(
    authenticated_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=Poll.PollResultTiming.AFTER_CLOSE,
        status=Poll.PollStatus.CLOSED,
        closed_at=timezone.now(),
    )
    response = authenticated_client.get(f"/api/polls/{poll.id}/results")
    assert response.status_code == 200
    body = response.json()
    assert "total_voters" in body


def _mc_poll_with_one_yes_vote(
    organization: Organization,
    voter: t.Any,
    *,
    staff_anonymous: bool,
    public_anonymous: bool,
    result_timing: Poll.PollResultTiming,
    status: Poll.PollStatus,
) -> tuple[Poll, MultipleChoiceQuestion, MultipleChoiceOption, MultipleChoiceOption]:
    """Build an MC poll ("Eggs": yes/no) with ``voter`` having picked "yes"."""
    q = Questionnaire.objects.create(name="eggs Q")
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Eggs")
    yes = MultipleChoiceOption.objects.create(question=mcq, option="yes")
    no = MultipleChoiceOption.objects.create(question=mcq, option="no")
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=result_timing,
        status=status,
        closed_at=timezone.now() if status == Poll.PollStatus.CLOSED else None,
        staff_anonymous=staff_anonymous,
        public_anonymous=public_anonymous,
    )
    sub = QuestionnaireSubmission.objects.create(
        user=voter,
        questionnaire=q,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    MultipleChoiceAnswer.objects.create(submission=sub, question=mcq, option=yes)
    return poll, mcq, yes, no


def test_results_expose_mc_voters_for_owner_when_not_staff_anonymous(
    owner_client: Client,
    organization: Organization,
    revel_user_factory: t.Any,
) -> None:
    """Owner of a staff_anonymous=false poll sees who picked each MC option (#450).

    Mirrors the repro: staff_anonymous=false + public_anonymous=true, viewed by
    the org owner. The picked option lists the voter; the unpicked option is [].
    """
    voter = revel_user_factory(preferred_name="Bob Voter")
    poll, _mcq, _yes, _no = _mc_poll_with_one_yes_vote(
        organization,
        voter,
        staff_anonymous=False,
        public_anonymous=True,
        result_timing=Poll.PollResultTiming.AFTER_VOTE,
        status=Poll.PollStatus.OPEN,
    )

    response = owner_client.get(f"/api/polls/{poll.id}/results")
    assert response.status_code == 200
    options = response.json()["mc_question_stats"][0]["options"]
    by_text = {o["option_text"]: o for o in options}
    assert by_text["yes"]["voters"] == [
        {"user_id": str(voter.id), "user_display_name": "Bob Voter", "user_email": voter.email}
    ]
    assert by_text["no"]["voters"] == []


def test_results_hide_mc_voters_from_non_staff_when_public_anonymous(
    authenticated_client: Client,
    organization: Organization,
    revel_user_factory: t.Any,
) -> None:
    """A non-staff viewer never sees MC voters on a public_anonymous poll (#450).

    staff_anonymous=false only lifts the veil for staff; for a public viewer
    public_anonymous=true keeps every option's ``voters`` null.
    """
    voter = revel_user_factory(preferred_name="Bob Voter")
    poll, _mcq, _yes, _no = _mc_poll_with_one_yes_vote(
        organization,
        voter,
        staff_anonymous=False,
        public_anonymous=True,
        result_timing=Poll.PollResultTiming.AFTER_CLOSE,
        status=Poll.PollStatus.CLOSED,
    )

    response = authenticated_client.get(f"/api/polls/{poll.id}/results")
    assert response.status_code == 200
    options = response.json()["mc_question_stats"][0]["options"]
    assert all(o["voters"] is None for o in options)
