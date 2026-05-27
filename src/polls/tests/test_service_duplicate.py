"""Unit tests for poll_service.duplicate_poll."""

import typing as t

import pytest
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import MembershipTier, Organization
from polls.exceptions import PollResultsMustBeAnonymousError
from polls.models import Poll
from polls.schema import PollCreateSchema
from polls.service import poll_service
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


def _create_payload(organization: Organization, **overrides: t.Any) -> PollCreateSchema:
    base: dict[str, t.Any] = {
        "name": "Template Poll",
        "vote_visibility": ResourceVisibility.PUBLIC,
        "result_visibility": ResourceVisibility.PUBLIC,
        "result_timing": Poll.PollResultTiming.AFTER_VOTE,
        "staff_anonymous": True,
        "public_anonymous": True,
    }
    base.update(overrides)
    return PollCreateSchema(**base)


# --- Core duplication behaviour ---


def test_duplicate_creates_new_poll_in_draft(organization: Organization) -> None:
    template = poll_service.create_poll(organization, _create_payload(organization))
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert duplicate.pk != template.pk
    assert duplicate.status == Poll.PollStatus.DRAFT


def test_duplicate_new_questionnaire_distinct_from_template(organization: Organization) -> None:
    """The duplicated poll must reference a NEW questionnaire, not the original."""
    template = poll_service.create_poll(organization, _create_payload(organization))
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert duplicate.questionnaire_id != template.questionnaire_id
    assert Questionnaire.objects.filter(pk=duplicate.questionnaire_id).exists()


def test_duplicate_new_questionnaire_has_questions(organization: Organization) -> None:
    """Questions present in the template questionnaire are copied to the new one."""
    template = poll_service.create_poll(organization, _create_payload(organization))
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=template.questionnaire, question="Favourite colour?")
    MultipleChoiceOption.objects.create(question=mcq, option="Red")
    MultipleChoiceOption.objects.create(question=mcq, option="Blue")

    duplicate = poll_service.duplicate_poll(template, "Copy With Questions")

    new_qs = duplicate.questionnaire
    new_mc_questions = list(new_qs.multiplechoicequestion_questions.prefetch_related("options"))
    assert len(new_mc_questions) == 1
    new_q = new_mc_questions[0]
    assert new_q.pk != mcq.pk  # distinct row
    assert new_q.question == mcq.question
    option_texts = sorted(opt.option for opt in new_q.options.all())
    assert option_texts == ["Blue", "Red"]


def test_duplicate_questionnaire_name_set_to_new_name(organization: Organization) -> None:
    template = poll_service.create_poll(organization, _create_payload(organization, name="Original"))
    duplicate = poll_service.duplicate_poll(template, "Renamed Copy")

    assert duplicate.questionnaire.name == "Renamed Copy"


def test_duplicate_forces_evaluation_mode_manual(organization: Organization) -> None:
    """The new questionnaire must have evaluation_mode=MANUAL even if the helper ever changes."""
    template = poll_service.create_poll(organization, _create_payload(organization))
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert duplicate.questionnaire.evaluation_mode == Questionnaire.QuestionnaireEvaluationMode.MANUAL


# --- Config fields copied ---


def test_duplicate_copies_config_fields(organization: Organization) -> None:
    template = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            vote_visibility=ResourceVisibility.MEMBERS_ONLY,
            result_visibility=ResourceVisibility.STAFF_ONLY,
            result_timing=Poll.PollResultTiming.AFTER_CLOSE,
            allow_vote_changes=True,
            staff_anonymous=False,
            public_anonymous=True,
        ),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert duplicate.organization_id == template.organization_id
    assert duplicate.vote_visibility == template.vote_visibility
    assert duplicate.result_visibility == template.result_visibility
    assert duplicate.result_timing == template.result_timing
    assert duplicate.allow_vote_changes == template.allow_vote_changes
    assert duplicate.staff_anonymous == template.staff_anonymous
    assert duplicate.public_anonymous == template.public_anonymous


def test_duplicate_copies_m2m_membership_tiers(organization: Organization) -> None:
    tier_a = MembershipTier.objects.create(organization=organization, name="A")
    tier_b = MembershipTier.objects.create(organization=organization, name="B")

    template = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            vote_membership_tier_ids=[tier_a.id],
            result_membership_tier_ids=[tier_b.id],
        ),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert set(duplicate.vote_membership_tiers.values_list("id", flat=True)) == {tier_a.id}
    assert set(duplicate.result_membership_tiers.values_list("id", flat=True)) == {tier_b.id}


def test_duplicate_copies_event_fk(organization: Organization, event: t.Any) -> None:
    template = poll_service.create_poll(
        organization,
        _create_payload(organization, event_id=event.id),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert duplicate.event_id == template.event_id


# --- Lifecycle reset ---


def test_duplicate_lifecycle_reset(organization: Organization) -> None:
    """Duplicating an OPEN poll produces a DRAFT duplicate with no timestamps."""
    template = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(template)
    template.refresh_from_db()
    assert template.status == Poll.PollStatus.OPEN
    assert template.opened_at is not None

    duplicate = poll_service.duplicate_poll(template, "Copy Of Open")

    assert duplicate.status == Poll.PollStatus.DRAFT
    assert duplicate.opened_at is None
    assert duplicate.closed_at is None
    assert duplicate.closes_at is None


def test_duplicate_closed_poll_lifecycle_reset(organization: Organization) -> None:
    """Duplicating a CLOSED poll produces a DRAFT duplicate."""
    template = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(template)
    poll_service.close_poll(template)
    template.refresh_from_db()
    assert template.status == Poll.PollStatus.CLOSED

    duplicate = poll_service.duplicate_poll(template, "Copy Of Closed")

    assert duplicate.status == Poll.PollStatus.DRAFT
    assert duplicate.opened_at is None
    assert duplicate.closed_at is None


def test_duplicate_original_unchanged(organization: Organization) -> None:
    """Duplicating a poll must not mutate the template."""
    template = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(template)
    original_status = Poll.PollStatus.OPEN

    poll_service.duplicate_poll(template, "Copy")

    template.refresh_from_db()
    assert template.status == original_status


# --- Votes not copied ---


def test_duplicate_does_not_copy_votes(organization: Organization, revel_user_factory: t.Any) -> None:
    template = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(template)
    user = revel_user_factory()
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=template.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    assert QuestionnaireSubmission.objects.filter(questionnaire=template.questionnaire).count() == 1

    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert QuestionnaireSubmission.objects.filter(questionnaire=duplicate.questionnaire).count() == 0


# --- Anonymity override ---


def test_duplicate_anonymity_copied_when_not_overridden(organization: Organization) -> None:
    """When staff_anonymous/public_anonymous are not passed, template values are copied."""
    template = poll_service.create_poll(
        organization,
        _create_payload(organization, staff_anonymous=False, public_anonymous=True),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy")

    assert duplicate.staff_anonymous is False
    assert duplicate.public_anonymous is True


def test_duplicate_anonymity_override_applied(organization: Organization) -> None:
    """When overrides are supplied they take precedence over the template values.

    ``public_anonymous=False`` is only valid when result_visibility is NOT
    PUBLIC/UNLISTED, so we use STAFF_ONLY here.
    """
    template = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            result_visibility=ResourceVisibility.STAFF_ONLY,
            staff_anonymous=True,
            public_anonymous=True,
        ),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy", staff_anonymous=False, public_anonymous=False)

    assert duplicate.staff_anonymous is False
    assert duplicate.public_anonymous is False


def test_duplicate_partial_anonymity_override(organization: Organization) -> None:
    """Only one override may be supplied; the other copies from the template."""
    template = poll_service.create_poll(
        organization,
        _create_payload(organization, staff_anonymous=True, public_anonymous=True),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy", staff_anonymous=False)

    assert duplicate.staff_anonymous is False
    assert duplicate.public_anonymous is True  # copied from template


# --- Constraint violation guard ---


def test_duplicate_override_public_anonymous_false_with_public_result_visibility_raises(
    organization: Organization,
) -> None:
    """public_anonymous=False override when result_visibility=PUBLIC must raise PollResultsMustBeAnonymousError."""
    template = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            result_visibility=ResourceVisibility.PUBLIC,
            public_anonymous=True,
        ),
    )
    with pytest.raises(PollResultsMustBeAnonymousError):
        poll_service.duplicate_poll(template, "Bad Copy", public_anonymous=False)


def test_duplicate_override_public_anonymous_false_with_unlisted_result_visibility_raises(
    organization: Organization,
) -> None:
    """public_anonymous=False override when result_visibility=UNLISTED must raise PollResultsMustBeAnonymousError."""
    template = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            result_visibility=ResourceVisibility.STAFF_ONLY,
            public_anonymous=True,
        ),
    )
    # Manually change result_visibility to UNLISTED in DB (bypassing model save guard
    # which only checks anonymity immutability, not this constraint on create).
    Poll.objects.filter(pk=template.pk).update(result_visibility=ResourceVisibility.UNLISTED)
    template.refresh_from_db()

    with pytest.raises(PollResultsMustBeAnonymousError):
        poll_service.duplicate_poll(template, "Bad Copy", public_anonymous=False)


def test_duplicate_valid_override_with_non_public_result_visibility_succeeds(
    organization: Organization,
) -> None:
    """public_anonymous=False is fine when result_visibility is not PUBLIC/UNLISTED."""
    template = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            result_visibility=ResourceVisibility.STAFF_ONLY,
            public_anonymous=True,
        ),
    )
    duplicate = poll_service.duplicate_poll(template, "Copy", public_anonymous=False)

    assert duplicate.public_anonymous is False
    assert duplicate.result_visibility == ResourceVisibility.STAFF_ONLY
