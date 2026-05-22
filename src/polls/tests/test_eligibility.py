"""Unit tests for polls.service.eligibility."""

import typing as t

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from events.models.event import Event
from events.models.invitation import EventInvitation
from events.models.mixins import ResourceVisibility
from events.models.organization import (
    MembershipTier,
    Organization,
    OrganizationMember,
)
from polls.models import Poll
from polls.service import eligibility
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


# --- can_vote ---


def test_can_vote_returns_false_for_anonymous_user(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    assert eligibility.can_vote(AnonymousUser(), poll) is False


def test_can_vote_unlisted_authenticated_passes(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.UNLISTED,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_vote(user, poll) is True


def test_can_vote_members_only_blocks_non_members(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_vote(user, poll) is False


def test_can_vote_members_only_allows_active_member(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    OrganizationMember.objects.create(
        user=user,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    assert eligibility.can_vote(user, poll) is True


def test_can_vote_members_only_empty_tiers_allows_any_member(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """MEMBERS_ONLY with empty vote_membership_tiers ⇒ any active member can vote."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    assert poll.vote_membership_tiers.exists() is False  # explicit precondition
    user = revel_user_factory()
    OrganizationMember.objects.create(
        user=user,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    assert eligibility.can_vote(user, poll) is True


def test_can_vote_members_only_with_tier_restriction(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """When vote_membership_tiers is non-empty, only members in those tiers can vote."""
    tier_a = MembershipTier.objects.create(organization=organization, name="A")
    tier_b = MembershipTier.objects.create(organization=organization, name="B")

    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    poll.vote_membership_tiers.add(tier_a)

    user_a = revel_user_factory()
    OrganizationMember.objects.create(
        user=user_a,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
        tier=tier_a,
    )
    user_b = revel_user_factory()
    OrganizationMember.objects.create(
        user=user_b,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
        tier=tier_b,
    )

    assert eligibility.can_vote(user_a, poll) is True
    assert eligibility.can_vote(user_b, poll) is False


def test_can_vote_private_with_invitation(
    organization: Organization,
    questionnaire: Questionnaire,
    event: Event,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        event=event,
        vote_visibility=ResourceVisibility.PRIVATE,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    EventInvitation.objects.create(user=user, event=event)
    assert eligibility.can_vote(user, poll) is True


def test_can_vote_attendees_only_blocks_invitee_without_ticket(
    organization: Organization,
    questionnaire: Questionnaire,
    event: Event,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        event=event,
        vote_visibility=ResourceVisibility.ATTENDEES_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    EventInvitation.objects.create(user=user, event=event)  # invited but no ticket / RSVP
    assert eligibility.can_vote(user, poll) is False


def test_can_vote_staff_only_blocks_member(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.STAFF_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    OrganizationMember.objects.create(
        user=user,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )
    assert eligibility.can_vote(user, poll) is False


def test_can_vote_owner_always_passes(
    organization: Organization,
    questionnaire: Questionnaire,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.STAFF_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    assert eligibility.can_vote(organization.owner, poll) is True


# --- can_see_results ---


def test_can_see_results_after_close_blocks_when_open(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=Poll.PollResultTiming.AFTER_CLOSE,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_see_results(user, poll) is False


def test_can_see_results_after_close_allows_when_closed(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
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
    user = revel_user_factory()
    assert eligibility.can_see_results(user, poll) is True


def test_can_see_results_never_blocks_non_staff(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
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
    user = revel_user_factory()
    assert eligibility.can_see_results(user, poll) is False


def test_can_see_results_staff_owner_always_passes(
    organization: Organization,
    questionnaire: Questionnaire,
) -> None:
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        result_timing=Poll.PollResultTiming.NEVER,
        status=Poll.PollStatus.OPEN,
    )
    assert eligibility.can_see_results(organization.owner, poll) is True


def test_can_see_results_after_vote_requires_submission(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """A non-staff user with AFTER_VOTE only sees results once they have a READY submission."""
    from questionnaires.models import QuestionnaireSubmission

    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=Poll.PollResultTiming.AFTER_VOTE,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_see_results(user, poll) is False
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    assert eligibility.can_see_results(user, poll) is True


# --- can_see_poll ---


def test_can_see_poll_returns_true_when_user_can_vote(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """PUBLIC vote_visibility, restrictive result_visibility ⇒ visible because user can vote."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        result_timing=Poll.PollResultTiming.NEVER,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_see_poll(user, poll) is True


def test_can_see_poll_returns_true_when_user_can_see_results(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """User passes result_visibility but not vote_visibility ⇒ poll still visible."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.STAFF_ONLY,
        result_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_vote(user, poll) is False
    assert eligibility.can_see_poll(user, poll) is True


def test_can_see_poll_returns_true_when_user_has_voted(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """A user with a READY submission keeps visibility even when audience would exclude them."""
    from questionnaires.models import QuestionnaireSubmission

    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.STAFF_ONLY,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        result_timing=Poll.PollResultTiming.NEVER,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    # Sanity: without a submission, the user cannot see the poll.
    assert eligibility.can_see_poll(user, poll) is False
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    assert eligibility.can_see_poll(user, poll) is True


def test_can_see_poll_returns_false_for_outsider(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """No membership, no invitation, no submission, MEMBERS_ONLY visibility ⇒ not visible."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        result_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert eligibility.can_see_poll(user, poll) is False


def test_can_see_poll_anonymous_with_public_vote_visibility(
    organization: Organization,
    questionnaire: Questionnaire,
) -> None:
    """Anonymous user, PUBLIC vote_visibility + restrictive result_visibility ⇒ visible.

    Regression: previously ``can_see_poll`` gated through ``can_vote`` which
    hard-blocks anonymous users, hiding the poll even though its vote audience
    is public.
    """
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        result_timing=Poll.PollResultTiming.NEVER,
        status=Poll.PollStatus.OPEN,
    )
    assert eligibility.can_vote(AnonymousUser(), poll) is False  # sanity
    assert eligibility.can_see_poll(AnonymousUser(), poll) is True


# --- Annotation-based listing helpers: Django-staff regression ---


def test_with_user_annotations_django_staff_after_vote(
    organization: Organization,
    questionnaire: Questionnaire,
    revel_user_factory: t.Any,
) -> None:
    """Django staff/superusers see AFTER_VOTE results once they have voted.

    Regression: the previous bulk-context precompute left ``voted_questionnaire_ids``
    empty for staff users, which made ``bulk_can_see_results`` return False for
    a staff member with AFTER_VOTE timing even after they had voted. The
    annotation-based listing path must keep that case working.
    """
    from questionnaires.models import QuestionnaireSubmission

    staff = revel_user_factory()
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])

    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=Poll.PollResultTiming.AFTER_VOTE,
        status=Poll.PollStatus.OPEN,
    )
    QuestionnaireSubmission.objects.create(
        user=staff,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )

    annotated = Poll.objects.with_user_annotations(staff).get(pk=poll.pk)
    assert eligibility.user_has_voted_from_annotations(staff, annotated) is True
    assert eligibility.can_see_results_from_annotations(staff, annotated, []) is True
