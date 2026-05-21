"""Tests for Poll.objects.for_user — visibility-aware listing."""

import typing as t

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization, OrganizationMember, OrganizationStaff
from polls.models import Poll
from questionnaires.models import QuestionnaireSubmission

pytestmark = pytest.mark.django_db


def test_for_user_public_poll_visible_to_anonymous(organization: t.Any, questionnaire: t.Any) -> None:
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    qs = Poll.objects.for_user(AnonymousUser())
    assert qs.count() == 1


def test_for_user_members_only_hidden_to_non_member(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.MEMBERS_ONLY,
        status=Poll.PollStatus.OPEN,
    )
    user = revel_user_factory()
    assert Poll.objects.for_user(user).count() == 0


def test_for_user_voted_poll_always_visible(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    """Even when audience is tightened, a user who voted retains visibility."""
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.STAFF_ONLY,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        status=Poll.PollStatus.CLOSED,
        closed_at=timezone.now(),
    )
    user = revel_user_factory()
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    qs = Poll.objects.for_user(user)
    assert qs.filter(pk=poll.pk).exists()


def test_for_user_draft_hidden_from_non_staff(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    user = revel_user_factory()
    assert Poll.objects.for_user(user).count() == 0


def test_for_user_draft_visible_to_owner(organization: t.Any, questionnaire: t.Any) -> None:
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    qs = Poll.objects.for_user(organization.owner)
    assert qs.count() == 1


def test_for_user_draft_visible_to_org_staff(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    """Non-owner staff of the org must see DRAFT polls."""
    staff_user = revel_user_factory()
    OrganizationStaff.objects.create(organization=organization, user=staff_user)
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    assert Poll.objects.for_user(staff_user).filter(pk=poll.pk).exists()


def test_for_user_draft_hidden_from_other_org_staff(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    """Staff of a *different* org must NOT see DRAFT polls of the first org."""
    other_owner = revel_user_factory()
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=other_owner)
    other_org_staff = revel_user_factory()
    OrganizationStaff.objects.create(organization=other_org, user=other_org_staff)
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )
    assert Poll.objects.for_user(other_org_staff).filter(pk=poll.pk).count() == 0


def test_for_user_banned_member_does_not_see_public_poll(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    """A user banned from the org must not see public polls belonging to it."""
    user = revel_user_factory()
    OrganizationMember.objects.create(
        organization=organization, user=user, status=OrganizationMember.MembershipStatus.BANNED
    )
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
    )
    assert Poll.objects.for_user(user).count() == 0


def test_for_user_banned_member_does_not_see_voted_poll(
    organization: t.Any, questionnaire: t.Any, revel_user_factory: t.Any
) -> None:
    """A banned user with a stale READY submission must still be excluded."""
    user = revel_user_factory()
    OrganizationMember.objects.create(
        organization=organization, user=user, status=OrganizationMember.MembershipStatus.BANNED
    )
    Poll.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        vote_visibility=ResourceVisibility.STAFF_ONLY,
        result_visibility=ResourceVisibility.STAFF_ONLY,
        status=Poll.PollStatus.CLOSED,
        closed_at=timezone.now(),
    )
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    assert Poll.objects.for_user(user).count() == 0
