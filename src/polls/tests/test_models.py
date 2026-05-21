"""Unit tests for the Poll model."""

import typing as t

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from polls.exceptions import PollAnonymityImmutableError
from polls.models import Poll

pytestmark = pytest.mark.django_db


def _create_poll(**overrides: t.Any) -> Poll:
    """Create a Poll with reasonable defaults.

    Caller supplies ``organization``, ``questionnaire``, and optionally ``event``.
    """
    defaults: dict[str, t.Any] = {
        "status": Poll.PollStatus.DRAFT,
        "vote_visibility": ResourceVisibility.MEMBERS_ONLY,
        "result_visibility": ResourceVisibility.STAFF_ONLY,
        "result_timing": Poll.PollResultTiming.NEVER,
        "staff_anonymous": True,
        "public_anonymous": True,
        "allow_vote_changes": False,
    }
    defaults.update(overrides)
    return Poll.objects.create(**defaults)


def test_poll_defaults(organization: t.Any, questionnaire: t.Any) -> None:
    """A newly created Poll has the documented defaults."""
    poll = _create_poll(organization=organization, questionnaire=questionnaire)
    assert poll.status == Poll.PollStatus.DRAFT
    assert poll.staff_anonymous is True
    assert poll.public_anonymous is True
    assert poll.allow_vote_changes is False
    assert poll.opened_at is None
    assert poll.closed_at is None


def test_poll_questionnaire_is_one_to_one(organization: t.Any, questionnaire: t.Any) -> None:
    """A Questionnaire can back at most one Poll."""
    _create_poll(organization=organization, questionnaire=questionnaire)
    # ``full_clean`` in ``TimeStampedModel.save`` surfaces the uniqueness violation
    # as ``ValidationError`` before the DB-level IntegrityError can fire.
    with pytest.raises(ValidationError):
        _create_poll(organization=organization, questionnaire=questionnaire)


def test_check_constraint_public_results_must_be_anonymous(organization: t.Any, questionnaire: t.Any) -> None:
    """PUBLIC result visibility requires public_anonymous=True."""
    with pytest.raises(ValidationError):
        _create_poll(
            organization=organization,
            questionnaire=questionnaire,
            result_visibility=ResourceVisibility.PUBLIC,
            public_anonymous=False,
        )


def test_check_constraint_unlisted_results_must_be_anonymous(organization: t.Any, questionnaire: t.Any) -> None:
    """UNLISTED result visibility requires public_anonymous=True."""
    with pytest.raises(ValidationError):
        _create_poll(
            organization=organization,
            questionnaire=questionnaire,
            result_visibility=ResourceVisibility.UNLISTED,
            public_anonymous=False,
        )


def test_check_constraint_attendees_only_requires_event(organization: t.Any, questionnaire: t.Any) -> None:
    """ATTENDEES_ONLY vote/result visibility requires an event."""
    with pytest.raises(ValidationError):
        _create_poll(
            organization=organization,
            questionnaire=questionnaire,
            vote_visibility=ResourceVisibility.ATTENDEES_ONLY,
        )


def test_check_constraint_private_requires_event(organization: t.Any, questionnaire: t.Any) -> None:
    """PRIVATE vote/result visibility requires an event."""
    with pytest.raises(ValidationError):
        _create_poll(
            organization=organization,
            questionnaire=questionnaire,
            vote_visibility=ResourceVisibility.PRIVATE,
        )


def test_attendees_only_with_event_succeeds(organization: t.Any, questionnaire: t.Any, event: t.Any) -> None:
    """An event-bound poll may use ATTENDEES_ONLY visibility."""
    poll = _create_poll(
        organization=organization,
        questionnaire=questionnaire,
        event=event,
        vote_visibility=ResourceVisibility.ATTENDEES_ONLY,
    )
    assert poll.vote_visibility == ResourceVisibility.ATTENDEES_ONLY


def test_staff_anonymous_immutable_after_create(organization: t.Any, questionnaire: t.Any) -> None:
    """staff_anonymous cannot be toggled after the poll is persisted."""
    poll = _create_poll(organization=organization, questionnaire=questionnaire)
    poll.staff_anonymous = False
    with pytest.raises(PollAnonymityImmutableError):
        poll.save()


def test_public_anonymous_immutable_after_create(organization: t.Any, questionnaire: t.Any) -> None:
    """public_anonymous cannot be toggled after the poll is persisted."""
    poll = _create_poll(organization=organization, questionnaire=questionnaire)
    poll.public_anonymous = False
    with pytest.raises(PollAnonymityImmutableError):
        poll.save()


def test_status_change_does_not_trigger_anonymity_lock(organization: t.Any, questionnaire: t.Any) -> None:
    """Changing fields other than the anonymity flags must not raise."""
    poll = _create_poll(organization=organization, questionnaire=questionnaire)
    poll.status = Poll.PollStatus.OPEN
    poll.opened_at = timezone.now()
    poll.save()  # must not raise
    poll.refresh_from_db()
    assert poll.status == Poll.PollStatus.OPEN
