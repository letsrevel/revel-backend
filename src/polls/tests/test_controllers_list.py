"""Controller-level tests for GET /polls/ and GET /polls/{id}/."""

import typing as t

import pytest
from django.test.client import Client

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def test_list_public_poll_visible_to_anonymous(
    anonymous_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    response = anonymous_client.get("/api/polls/")
    assert response.status_code == 200
    data: t.Any = response.json()
    if isinstance(data, dict):
        items: list[t.Any] = data.get("items") or data.get("results") or []
    else:
        items = data
    assert len(items) == 1


def test_list_members_only_hidden_from_non_member(
    authenticated_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    response = authenticated_client.get("/api/polls/")
    assert response.status_code == 200
    data: t.Any = response.json()
    if isinstance(data, dict):
        items: list[t.Any] = data.get("items") or data.get("results") or []
    else:
        items = data
    assert len(items) == 0


def test_detail_returns_403_for_invisible_poll(
    authenticated_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    """GET /polls/{id}/ must distinguish "exists but you can't see it" from "missing".

    The frontend voter URL relies on 404 meaning "no such poll" and 403 meaning
    "you don't have access" to render the right state (page-not-found vs.
    ineligible banner). Previously both surfaces collapsed to 404 because the
    detail queryset was visibility-filtered.
    """
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    response = authenticated_client.get(f"/api/polls/{poll.id}/")
    assert response.status_code == 403


def test_detail_returns_404_for_missing_poll(authenticated_client: Client) -> None:
    """Genuinely-missing polls still 404."""
    from uuid import uuid4

    response = authenticated_client.get(f"/api/polls/{uuid4()}/")
    assert response.status_code == 404


def test_detail_includes_user_flags(
    authenticated_client: Client, organization: Organization, questionnaire: Questionnaire
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    response = authenticated_client.get(f"/api/polls/{poll.id}/")
    assert response.status_code == 200
    body = response.json()
    assert "user_can_vote" in body
    assert "user_can_see_results" in body
    assert "user_has_voted" in body


def test_detail_includes_questionnaire_structure(
    authenticated_client: Client,
    organization: Organization,
) -> None:
    """`PollDetailSchema.questionnaire` carries the deep question/section tree.

    Frontend needs the questions + options to render the vote form when
    ``user_can_vote`` is True. Regression for the original TODO that returned
    ``questionnaire=None`` unconditionally (see POLLS_FRONTEND_ASK.md).
    """
    from questionnaires.models import (
        MultipleChoiceOption,
        MultipleChoiceQuestion,
        QuestionnaireSection,
    )

    q = Questionnaire.objects.create(name="poll Q")
    # Top-level (section-less) question
    top_mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Top?")
    MultipleChoiceOption.objects.create(question=top_mcq, option="a")
    MultipleChoiceOption.objects.create(question=top_mcq, option="b")
    # Section with a nested MC question
    section = QuestionnaireSection.objects.create(questionnaire=q, name="Section A", order=0)
    section_mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, section=section, question="Section question?")
    MultipleChoiceOption.objects.create(question=section_mcq, option="x")

    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )

    response = authenticated_client.get(f"/api/polls/{poll.id}/")
    assert response.status_code == 200
    body = response.json()

    assert body["questionnaire"] is not None
    qresp = body["questionnaire"]
    assert qresp["id"] == str(q.id)
    assert qresp["name"] == "poll Q"
    # Top-level question with options serialised
    assert len(qresp["multiple_choice_questions"]) == 1
    top = qresp["multiple_choice_questions"][0]
    assert top["question"] == "Top?"
    assert {opt["option"] for opt in top["options"]} == {"a", "b"}
    # Section with its nested question
    assert len(qresp["sections"]) == 1
    sec = qresp["sections"][0]
    assert sec["name"] == "Section A"
    assert len(sec["multiple_choice_questions"]) == 1
    assert sec["multiple_choice_questions"][0]["question"] == "Section question?"


def test_list_polls_query_count_constant(
    authenticated_client: Client,
    organization: Organization,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Listing 5 polls should NOT scale queries with the number of polls.

    Eligibility flags are bulk-precomputed: per-row work is set lookups, not
    ``.exists()`` queries. We measure query counts for two list sizes and
    assert the per-row delta is small (silk profiler attaches its own
    queries in test settings, so we can't pin an absolute number).
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    # Baseline: one poll.
    Poll.objects.create(
        organization=organization,
        questionnaire=Questionnaire.objects.create(name="q-baseline"),
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    with CaptureQueriesContext(connection) as ctx_one:
        response_one = authenticated_client.get("/api/polls/")
    assert response_one.status_code == 200
    one_poll_queries = len(ctx_one.captured_queries)

    # Scale up to five polls.
    for i in range(4):
        Poll.objects.create(
            organization=organization,
            questionnaire=Questionnaire.objects.create(name=f"q-{i}"),
            vote_visibility=ResourceVisibility.PUBLIC,
            status=Poll.PollStatus.OPEN,
        )
    with CaptureQueriesContext(connection) as ctx_five:
        response_five = authenticated_client.get("/api/polls/")
    assert response_five.status_code == 200
    five_poll_queries = len(ctx_five.captured_queries)

    # Each additional poll incurs at most a small constant
    # (silk profiler logs a couple of bookkeeping queries per request, so we
    # allow some slack — but NOT 3+ per row, which is what the N+1 bug did).
    additional_per_poll = (five_poll_queries - one_poll_queries) / 4
    assert additional_per_poll < 2, (
        f"List endpoint scaled too aggressively with poll count: "
        f"{one_poll_queries} queries for 1 poll, {five_poll_queries} for 5 "
        f"({additional_per_poll:.1f} per extra poll)."
    )


def test_list_polls_query_count_does_not_grow_with_total_polls(
    authenticated_client: Client,
    organization: Organization,
) -> None:
    """Pagination is enforced at the DB level.

    With ``page_size=2``, query count must NOT scale with the total number of
    polls in the table: the bulk-eligibility precompute should only run over
    the requested page slice, not the entire visible queryset.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for i in range(10):
        Poll.objects.create(
            organization=organization,
            questionnaire=Questionnaire.objects.create(name=f"q-page-{i}"),
            vote_visibility=ResourceVisibility.PUBLIC,
            status=Poll.PollStatus.OPEN,
        )

    with CaptureQueriesContext(connection) as ctx_small:
        small = authenticated_client.get("/api/polls/?page_size=2")
    assert small.status_code == 200
    small_body = small.json()
    assert len(small_body["results"]) == 2
    assert small_body["count"] == 10

    # Add ten more polls and request the same page; query count must stay flat.
    for i in range(10):
        Poll.objects.create(
            organization=organization,
            questionnaire=Questionnaire.objects.create(name=f"q-extra-{i}"),
            vote_visibility=ResourceVisibility.PUBLIC,
            status=Poll.PollStatus.OPEN,
        )
    with CaptureQueriesContext(connection) as ctx_big:
        big = authenticated_client.get("/api/polls/?page_size=2")
    assert big.status_code == 200
    big_body = big.json()
    assert len(big_body["results"]) == 2
    assert big_body["count"] == 20

    # Both requests return ``page_size`` rows; the bulk-eligibility precompute
    # must therefore do the same amount of work regardless of how many polls
    # exist in total. Silk and other middleware can attach non-deterministic
    # bookkeeping queries, so we allow a small constant of slack — what we
    # are guarding against is the previous behaviour where the per-request
    # query count scaled with the total visible polls.
    delta = len(ctx_big.captured_queries) - len(ctx_small.captured_queries)
    assert delta < 5, (
        f"Pagination did not bound the per-request workload: "
        f"{len(ctx_small.captured_queries)} queries for 10 polls, "
        f"{len(ctx_big.captured_queries)} for 20 (delta={delta}) — both at page_size=2."
    )
