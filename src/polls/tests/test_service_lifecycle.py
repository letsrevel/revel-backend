"""Lifecycle tests for poll_service (create / open / close / reopen / delete)."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from events.models.event import Event
from events.models.mixins import ResourceVisibility
from events.models.organization import MembershipTier, Organization
from polls.exceptions import PollLifecycleError, PollValidationError
from polls.models import Poll
from polls.schema import PollCreateSchema, PollReopenSchema, PollUpdateSchema
from polls.service import poll_service
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def _create_payload(organization: Organization, **overrides: t.Any) -> PollCreateSchema:
    # ``organization`` is now passed alongside the payload to ``create_poll``
    # (the controller takes it from the URL path). This helper keeps the
    # signature for callers that still need to spell out cross-org test cases.
    _ = organization  # parameter kept for callsite symmetry / readability
    base: dict[str, t.Any] = {
        "name": "My Poll",
        "vote_visibility": ResourceVisibility.PUBLIC,
        "result_visibility": ResourceVisibility.PUBLIC,
        "result_timing": Poll.PollResultTiming.AFTER_VOTE,
        "staff_anonymous": True,
        "public_anonymous": True,
    }
    base.update(overrides)
    return PollCreateSchema(**base)


def test_create_poll_forces_no_evaluation(organization: Organization) -> None:
    poll = poll_service.create_poll(organization, _create_payload(organization))
    # Polls never run the questionnaire evaluator pipeline — the underlying
    # questionnaire is pinned to MANUAL mode regardless of what the client
    # sent (``Questionnaire`` itself has no ``requires_evaluation`` flag; that
    # lives on the OrganizationQuestionnaire wrapper, which polls don't use).
    assert poll.questionnaire.evaluation_mode == Questionnaire.QuestionnaireEvaluationMode.MANUAL
    assert poll.status == Poll.PollStatus.DRAFT


def test_open_poll_sets_opened_at_and_status(organization: Organization) -> None:
    poll = poll_service.create_poll(organization, _create_payload(organization))
    opened = poll_service.open_poll(poll)
    assert opened.status == Poll.PollStatus.OPEN
    assert opened.opened_at is not None


def test_open_poll_from_closed_is_a_reopen_not_allowed(organization: Organization) -> None:
    poll = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(poll)
    poll_service.close_poll(poll)
    with pytest.raises(PollLifecycleError):
        poll_service.open_poll(poll)


def test_close_poll_sets_closed_at(organization: Organization) -> None:
    poll = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(poll)
    closed = poll_service.close_poll(poll)
    assert closed.status == Poll.PollStatus.CLOSED
    assert closed.closed_at is not None


def test_close_poll_when_draft_raises(organization: Organization) -> None:
    poll = poll_service.create_poll(organization, _create_payload(organization))
    with pytest.raises(PollLifecycleError):
        poll_service.close_poll(poll)


def test_reopen_requires_future_closes_at_or_explicit_clear(organization: Organization) -> None:
    poll = poll_service.create_poll(
        organization, _create_payload(organization, closes_at=(timezone.now() + timedelta(hours=1)))
    )
    poll_service.open_poll(poll)
    # Force the poll past its closes_at by mutating the DB row directly to simulate auto-close
    poll.refresh_from_db()
    poll.closes_at = timezone.now() - timedelta(hours=1)
    poll.save(update_fields=["closes_at"])
    poll_service.close_poll(poll)

    # Reopen with no new closes_at and no explicit clear -> raises
    with pytest.raises(PollLifecycleError):
        poll_service.reopen_poll(poll, PollReopenSchema())

    # Reopen with new future closes_at -> succeeds
    reopened = poll_service.reopen_poll(poll, PollReopenSchema(closes_at=timezone.now() + timedelta(hours=2)))
    assert reopened.status == Poll.PollStatus.OPEN
    assert reopened.closed_at is None
    assert reopened.closes_at is not None


def test_reopen_with_clear_closes_at(organization: Organization) -> None:
    poll = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(poll)
    poll_service.close_poll(poll)
    reopened = poll_service.reopen_poll(poll, PollReopenSchema(clear_closes_at=True))
    assert reopened.status == Poll.PollStatus.OPEN
    assert reopened.closes_at is None


def test_update_poll_partial_update_only_changes_specified_fields(organization: Organization) -> None:
    """Patching only ``allow_vote_changes`` must leave the other fields untouched."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            vote_visibility=ResourceVisibility.PUBLIC,
            result_visibility=ResourceVisibility.PUBLIC,
            result_timing=Poll.PollResultTiming.AFTER_VOTE,
            allow_vote_changes=False,
        ),
    )
    original_vote_visibility = poll.vote_visibility
    original_result_visibility = poll.result_visibility
    original_result_timing = poll.result_timing
    original_closes_at = poll.closes_at
    original_event_id = poll.event_id

    updated = poll_service.update_poll(poll, PollUpdateSchema(allow_vote_changes=True))

    assert updated.allow_vote_changes is True
    assert updated.vote_visibility == original_vote_visibility
    assert updated.result_visibility == original_result_visibility
    assert updated.result_timing == original_result_timing
    assert updated.closes_at == original_closes_at
    assert updated.event_id == original_event_id

    # And verify it persisted to the DB
    updated.refresh_from_db()
    assert updated.allow_vote_changes is True
    assert updated.vote_visibility == original_vote_visibility


def test_update_poll_with_event_id_changes_fk(organization: Organization, event: Event) -> None:
    """Patching ``event_id`` must update the FK column."""
    poll = poll_service.create_poll(organization, _create_payload(organization))
    assert poll.event_id is None

    start = timezone.now() + timedelta(days=14)
    other_event = Event.objects.create(
        organization=organization,
        name="Other Event",
        slug="other-poll-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=10,
        status="open",
        start=start,
        end=start + timedelta(hours=2),
        requires_ticket=True,
    )

    updated = poll_service.update_poll(poll, PollUpdateSchema(event_id=other_event.id))

    assert updated.event_id == other_event.id
    updated.refresh_from_db()
    assert updated.event_id == other_event.id


def test_update_poll_membership_tiers_set_replaces_existing(organization: Organization) -> None:
    """Passing ``vote_membership_tier_ids`` must replace the existing M2M set, not append."""
    tier_a = MembershipTier.objects.create(organization=organization, name="A")
    tier_b = MembershipTier.objects.create(organization=organization, name="B")

    poll = poll_service.create_poll(organization, _create_payload(organization, vote_membership_tier_ids=[tier_a.id]))
    assert set(poll.vote_membership_tiers.values_list("id", flat=True)) == {tier_a.id}

    updated = poll_service.update_poll(poll, PollUpdateSchema(vote_membership_tier_ids=[tier_b.id]))

    assert set(updated.vote_membership_tiers.values_list("id", flat=True)) == {tier_b.id}


def test_update_poll_empty_membership_tier_list_clears_m2m(organization: Organization) -> None:
    """Passing an empty list (not None) must clear the M2M — ``set([])`` semantics."""
    tier_a = MembershipTier.objects.create(organization=organization, name="A")
    tier_b = MembershipTier.objects.create(organization=organization, name="B")

    poll = poll_service.create_poll(
        organization, _create_payload(organization, vote_membership_tier_ids=[tier_a.id, tier_b.id])
    )
    assert poll.vote_membership_tiers.count() == 2

    updated = poll_service.update_poll(poll, PollUpdateSchema(vote_membership_tier_ids=[]))

    assert updated.vote_membership_tiers.exists() is False


def test_update_poll_no_op_when_payload_empty(organization: Organization) -> None:
    """An empty PATCH (all fields unset) must not mutate the poll."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            vote_visibility=ResourceVisibility.PUBLIC,
            result_visibility=ResourceVisibility.MEMBERS_ONLY,
            result_timing=Poll.PollResultTiming.AFTER_CLOSE,
            allow_vote_changes=True,
        ),
    )
    snapshot = {
        "vote_visibility": poll.vote_visibility,
        "result_visibility": poll.result_visibility,
        "result_timing": poll.result_timing,
        "allow_vote_changes": poll.allow_vote_changes,
        "closes_at": poll.closes_at,
        "event_id": poll.event_id,
        "updated_at": poll.updated_at,
    }

    updated = poll_service.update_poll(poll, PollUpdateSchema())

    # In-memory result matches snapshot
    assert updated.vote_visibility == snapshot["vote_visibility"]
    assert updated.result_visibility == snapshot["result_visibility"]
    assert updated.result_timing == snapshot["result_timing"]
    assert updated.allow_vote_changes == snapshot["allow_vote_changes"]
    assert updated.closes_at == snapshot["closes_at"]
    assert updated.event_id == snapshot["event_id"]

    # And no save happened — ``updated_at`` must be unchanged
    updated.refresh_from_db()
    assert updated.updated_at == snapshot["updated_at"]


def test_update_poll_name_and_description_apply_to_questionnaire(organization: Organization) -> None:
    """``name`` and ``description`` on PATCH update the wrapped Questionnaire."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(organization, name="Original name", description="Original description"),
    )
    assert poll.questionnaire.name == "Original name"
    assert poll.questionnaire.description == "Original description"

    poll_service.update_poll(
        poll,
        PollUpdateSchema(name="Updated name", description="Updated description"),
    )

    poll.refresh_from_db()
    poll.questionnaire.refresh_from_db()
    assert poll.questionnaire.name == "Updated name"
    assert poll.questionnaire.description == "Updated description"


def test_update_poll_can_clear_description_only(organization: Organization) -> None:
    """An explicit ``description=None`` clears the field; ``name=None`` is rejected."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(organization, name="A name", description="Has text"),
    )

    poll_service.update_poll(poll, PollUpdateSchema(description=None))

    poll.questionnaire.refresh_from_db()
    assert poll.questionnaire.description is None
    assert poll.questionnaire.name == "A name"  # untouched


def test_update_poll_rejects_null_name() -> None:
    """Clearing ``name`` is forbidden — ``Questionnaire.name`` is non-nullable."""
    with pytest.raises(ValueError, match="Cannot clear the poll name"):
        PollUpdateSchema(name=None)


def test_update_poll_name_works_when_open(organization: Organization) -> None:
    """Questionnaire metadata (name/description) is editable even past DRAFT.

    The signal lockdown only blocks question/option/section mutations,
    not the questionnaire's own metadata fields.
    """
    poll = poll_service.create_poll(organization, _create_payload(organization, name="Draft name"))
    poll_service.open_poll(poll)

    poll_service.update_poll(poll, PollUpdateSchema(name="Open name"))

    poll.questionnaire.refresh_from_db()
    assert poll.questionnaire.name == "Open name"


def test_reopen_with_future_closes_at_no_payload_succeeds(organization: Organization) -> None:
    """Reopening with an existing future ``closes_at`` and an empty payload must succeed."""
    future = timezone.now() + timedelta(hours=2)
    poll = poll_service.create_poll(organization, _create_payload(organization, closes_at=future))
    poll_service.open_poll(poll)
    # Close the poll without mutating ``closes_at`` so the existing deadline is
    # still in the future when we reopen.
    poll_service.close_poll(poll)

    reopened = poll_service.reopen_poll(poll, PollReopenSchema())

    assert reopened.status == Poll.PollStatus.OPEN
    assert reopened.closed_at is None
    assert reopened.closes_at is not None
    assert reopened.closes_at > timezone.now()


def test_delete_poll_cascades_to_questionnaire_and_submissions(
    organization: Organization, revel_user_factory: t.Any
) -> None:
    from questionnaires.models import QuestionnaireSubmission

    poll = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(poll)
    user = revel_user_factory()
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=poll.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    qid = poll.questionnaire_id
    poll_service.delete_poll(poll)

    assert not Poll.objects.filter(pk=poll.pk).exists()
    assert not Questionnaire.objects.filter(pk=qid).exists()
    assert not QuestionnaireSubmission.objects.filter(questionnaire_id=qid).exists()


# --- membership-tier validation (B1) ---


def test_create_poll_with_unknown_tier_id_raises(organization: Organization) -> None:
    """A bogus tier UUID must raise rather than silently dropping the value."""
    import uuid

    bogus = uuid.uuid4()
    with pytest.raises(PollValidationError):
        poll_service.create_poll(
            organization,
            _create_payload(organization, vote_membership_tier_ids=[bogus]),
        )


def test_create_poll_with_cross_org_tier_id_raises(organization: Organization, revel_user_factory: t.Any) -> None:
    """A tier from a different organization must raise."""
    other = Organization.objects.create(name="Other", slug="other-org-tiers", owner=revel_user_factory())
    foreign_tier = MembershipTier.objects.create(organization=other, name="FT")
    with pytest.raises(PollValidationError):
        poll_service.create_poll(
            organization,
            _create_payload(organization, vote_membership_tier_ids=[foreign_tier.id]),
        )


def test_update_poll_with_unknown_tier_id_raises(organization: Organization) -> None:
    """Update path must also reject unknown tier IDs."""
    import uuid

    poll = poll_service.create_poll(organization, _create_payload(organization))
    with pytest.raises(PollValidationError):
        poll_service.update_poll(
            poll,
            PollUpdateSchema(vote_membership_tier_ids=[uuid.uuid4()]),
        )


# --- event_id cross-org validation (A1) ---


def _make_event(organization: Organization, slug: str) -> Event:
    """Build a minimal Event owned by ``organization`` for cross-org tests."""
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="X",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=10,
        status="open",
        start=start,
        end=start + timedelta(hours=2),
        requires_ticket=True,
    )


def test_create_poll_with_cross_org_event_id_raises(organization: Organization, revel_user_factory: t.Any) -> None:
    """Attaching a cross-org event to a new poll must raise (no silent leak)."""
    other = Organization.objects.create(name="Other", slug="other-org-event-create", owner=revel_user_factory())
    foreign_event = _make_event(other, slug="foreign-event-create")
    with pytest.raises(PollValidationError):
        poll_service.create_poll(
            organization,
            _create_payload(organization, event_id=foreign_event.id),
        )


def test_create_poll_with_unknown_event_id_raises(organization: Organization) -> None:
    """A bogus event UUID must raise PollValidationError rather than 500ing."""
    import uuid

    with pytest.raises(PollValidationError):
        poll_service.create_poll(
            organization,
            _create_payload(organization, event_id=uuid.uuid4()),
        )


def test_update_poll_with_cross_org_event_id_raises(organization: Organization, revel_user_factory: t.Any) -> None:
    """Moving a poll to a cross-org event via PATCH must raise."""
    other = Organization.objects.create(name="Other", slug="other-org-event-update", owner=revel_user_factory())
    foreign_event = _make_event(other, slug="foreign-event-update")

    poll = poll_service.create_poll(organization, _create_payload(organization))
    with pytest.raises(PollValidationError):
        poll_service.update_poll(poll, PollUpdateSchema(event_id=foreign_event.id))

    poll.refresh_from_db()
    assert poll.event_id is None  # validation BEFORE save = no mutation
