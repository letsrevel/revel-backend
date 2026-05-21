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
