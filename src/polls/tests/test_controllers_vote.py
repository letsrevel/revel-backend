"""Controller tests for POST/DELETE /polls/{id}/vote."""

import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def votable_poll(organization: Organization) -> tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]]:
    """Create an OPEN poll with one MC question and two options.

    Question/options are created BEFORE the Poll because the T11 signal
    lockdown forbids structural mutations to a poll's questionnaire once
    the poll exists.
    """
    q = Questionnaire.objects.create(name="q")
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="?")
    options = [MultipleChoiceOption.objects.create(question=mcq, option=f"o-{i}") for i in range(2)]
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )
    return poll, mcq, options


def test_vote_succeeds(
    authenticated_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    poll, mcq, options = votable_poll
    response = authenticated_client.post(
        f"/api/polls/{poll.id}/vote",
        data={
            "mc_answers": [{"question_id": str(mcq.id), "option_ids": [str(options[0].id)]}],
            "free_text_answers": [],
            "file_upload_answers": [],
        },
        content_type="application/json",
    )
    assert response.status_code == 200


def test_vote_when_anonymous_blocked(
    anonymous_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    poll, _mcq, _options = votable_poll
    response = anonymous_client.post(
        f"/api/polls/{poll.id}/vote",
        data={"mc_answers": [], "free_text_answers": [], "file_upload_answers": []},
        content_type="application/json",
    )
    assert response.status_code in (401, 403)


def test_double_vote_returns_409(
    authenticated_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    poll, mcq, options = votable_poll
    payload: dict[str, t.Any] = {
        "mc_answers": [{"question_id": str(mcq.id), "option_ids": [str(options[0].id)]}],
        "free_text_answers": [],
        "file_upload_answers": [],
    }
    first = authenticated_client.post(f"/api/polls/{poll.id}/vote", data=payload, content_type="application/json")
    assert first.status_code == 200
    response = authenticated_client.post(f"/api/polls/{poll.id}/vote", data=payload, content_type="application/json")
    assert response.status_code == 409


def test_withdraw_vote_when_allowed(
    authenticated_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    poll, mcq, options = votable_poll
    poll.allow_vote_changes = True
    poll.save(update_fields=["allow_vote_changes"])
    cast = authenticated_client.post(
        f"/api/polls/{poll.id}/vote",
        data={
            "mc_answers": [{"question_id": str(mcq.id), "option_ids": [str(options[0].id)]}],
            "free_text_answers": [],
            "file_upload_answers": [],
        },
        content_type="application/json",
    )
    assert cast.status_code == 200
    response = authenticated_client.delete(f"/api/polls/{poll.id}/vote", content_type="application/json")
    assert response.status_code == 204


def test_withdraw_vote_when_changes_disallowed_returns_403(
    authenticated_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    """Withdraw on a poll with ``allow_vote_changes=False`` returns 403, not 409.

    The semantic is "this poll's policy forbids withdrawing" — a permission
    issue, not a conflict.
    """
    poll, mcq, options = votable_poll
    # Cast a vote first so there is something to withdraw.
    cast = authenticated_client.post(
        f"/api/polls/{poll.id}/vote",
        data={
            "mc_answers": [{"question_id": str(mcq.id), "option_ids": [str(options[0].id)]}],
            "free_text_answers": [],
            "file_upload_answers": [],
        },
        content_type="application/json",
    )
    assert cast.status_code == 200
    # ``allow_vote_changes`` is False on this fixture.
    response = authenticated_client.delete(f"/api/polls/{poll.id}/vote", content_type="application/json")
    assert response.status_code == 403


def test_vote_with_unknown_question_returns_422(
    authenticated_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    """POSTing a bogus question_id returns 422, not 500 (DoesNotExist would 500)."""
    import uuid

    poll, _mcq, options = votable_poll
    response = authenticated_client.post(
        f"/api/polls/{poll.id}/vote",
        data={
            "mc_answers": [{"question_id": str(uuid.uuid4()), "option_ids": [str(options[0].id)]}],
            "free_text_answers": [],
            "file_upload_answers": [],
        },
        content_type="application/json",
    )
    assert response.status_code == 422


def test_vote_response_includes_results_when_after_vote(
    authenticated_client: Client,
    votable_poll: tuple[Poll, MultipleChoiceQuestion, list[MultipleChoiceOption]],
) -> None:
    """When ``result_timing=AFTER_VOTE``, the vote response body must include results.

    With PUBLIC result visibility the just-voted user can immediately see
    aggregate results, and the controller's detail response embeds them.
    """
    poll, mcq, options = votable_poll
    poll.result_visibility = ResourceVisibility.PUBLIC
    poll.result_timing = Poll.PollResultTiming.AFTER_VOTE
    poll.save(update_fields=["result_visibility", "result_timing"])

    response = authenticated_client.post(
        f"/api/polls/{poll.id}/vote",
        data={
            "mc_answers": [{"question_id": str(mcq.id), "option_ids": [str(options[0].id)]}],
            "free_text_answers": [],
            "file_upload_answers": [],
        },
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["results"] is not None
    assert body["results"]["total_voters"] >= 1
