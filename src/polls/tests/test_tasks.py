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


@pytest.mark.django_db(transaction=True)
def test_close_polls_due_processes_entire_batch_in_one_run(organization: Organization) -> None:
    """Regression for #458: every overdue poll is closed in a single run.

    Runs with real per-row COMMITs (``transaction=True``) over a multi-row
    batch. The original bug (shared with the subscription-expiry task) streamed
    a server-side cursor and crashed once a mid-loop commit recycled the pooled
    backend, leaving later rows untouched. The pooler-specific
    ``InvalidCursorName`` cannot be reproduced without PgBouncer — the
    ``DISABLE_SERVER_SIDE_CURSORS`` settings guardrail covers that — so this
    asserts the behavioural invariant: the whole batch is closed.
    """
    polls = [
        Poll.objects.create(
            organization=organization,
            questionnaire=Questionnaire.objects.create(name=f"batch-q-{i}"),
            vote_visibility=ResourceVisibility.PUBLIC,
            status=Poll.PollStatus.OPEN,
            opened_at=timezone.now() - timedelta(hours=2),
            closes_at=timezone.now() - timedelta(hours=1),
        )
        for i in range(5)
    ]

    closed = close_polls_due()

    assert closed == 5
    for poll in polls:
        poll.refresh_from_db()
        assert poll.status == Poll.PollStatus.CLOSED
