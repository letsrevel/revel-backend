"""Tests for GET /polls/{id}/results."""

import pytest
from django.test.client import Client
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import Questionnaire

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
