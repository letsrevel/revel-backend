"""Tests for polls.tasks.close_polls_due."""

from datetime import timedelta

import pytest
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from polls.tasks import close_polls_due
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def test_close_polls_due_closes_only_overdue_open_polls(
    organization: Organization, questionnaire: Questionnaire
) -> None:
    overdue = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now() - timedelta(hours=2),
        closes_at=timezone.now() - timedelta(hours=1),
    )
    other_q = Questionnaire.objects.create(name="other")
    not_overdue = Poll.objects.create(
        organization=organization,
        questionnaire=other_q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
        closes_at=timezone.now() + timedelta(hours=1),
    )

    close_polls_due()

    overdue.refresh_from_db()
    not_overdue.refresh_from_db()
    assert overdue.status == Poll.PollStatus.CLOSED
    assert not_overdue.status == Poll.PollStatus.OPEN


def test_close_polls_due_is_idempotent(organization: Organization, questionnaire: Questionnaire) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now() - timedelta(hours=2),
        closes_at=timezone.now() - timedelta(hours=1),
    )
    close_polls_due()
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.CLOSED
    closed_at_first = poll.closed_at
    close_polls_due()
    poll.refresh_from_db()
    assert poll.closed_at == closed_at_first  # not bumped on the second run
