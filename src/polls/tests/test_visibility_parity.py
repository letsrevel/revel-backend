"""Parity tests between the instance-path and annotation-path eligibility helpers.

``polls.service.eligibility`` carries two implementations of the same rules:

* the **instance path** (``_passes_visibility`` / ``can_see_results``), which queries the
  DB per call and backs the detail endpoints, and
* the **annotation path** (``passes_visibility_from_annotations`` /
  ``can_see_results_from_annotations``), which reads the per-row flags written by
  :meth:`polls.models.PollQuerySet.with_user_annotations` and backs the list endpoint.

They are declared exact counterparts, so any divergence is a bug: the same poll would be
listed and detailed with different permissions. These tests pin the equivalence across
every :class:`ResourceVisibility` value and every result-timing value.

Deliberately NOT asserted here: ``Poll.objects.for_user`` vs :func:`can_see_poll`. That
pair is intentionally coarser (``for_user`` does not refine tier-restricted MEMBERS_ONLY
polls) — see the ``for_user`` docstring.
"""

import typing as t
from dataclasses import dataclass

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from accounts.models import RevelUser
from events.models.event import Event
from events.models.invitation import EventInvitation
from events.models.mixins import ResourceVisibility
from events.models.organization import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
)
from events.models.rsvp import EventRSVP
from events.models.ticket import Ticket, TicketTier
from polls.models import Poll
from polls.service import eligibility
from polls.types import UserLike
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


@dataclass
class World:
    """The fixture world shared by both parity tests."""

    organization: Organization
    event: Event
    tier_a: MembershipTier
    tier_b: MembershipTier
    users: dict[str, UserLike]


# Every user state whose signal one of the two implementations reads.
USER_STATES: list[str] = [
    "anonymous",
    "unrelated",
    "owner",
    "org_staff",
    "member_no_tier",
    "member_tier_a",
    "member_tier_b",
    "ticket_holder",
    "rsvp",
    "invited",
]


@pytest.fixture
def world(organization: Organization, event: Event, revel_user_factory: t.Any) -> World:
    """Build one user per state the visibility rules can distinguish."""
    tier_a = MembershipTier.objects.create(organization=organization, name="Tier A")
    tier_b = MembershipTier.objects.create(organization=organization, name="Tier B")

    unrelated: RevelUser = revel_user_factory()

    org_staff: RevelUser = revel_user_factory()
    OrganizationStaff.objects.create(
        organization=organization,
        user=org_staff,
        permissions=PermissionsSchema(default=PermissionMap(manage_polls=True)).model_dump(mode="json"),
    )

    member_no_tier: RevelUser = revel_user_factory()
    OrganizationMember.objects.create(
        user=member_no_tier, organization=organization, status=OrganizationMember.MembershipStatus.ACTIVE
    )
    member_tier_a: RevelUser = revel_user_factory()
    OrganizationMember.objects.create(
        user=member_tier_a,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
        tier=tier_a,
    )
    member_tier_b: RevelUser = revel_user_factory()
    OrganizationMember.objects.create(
        user=member_tier_b,
        organization=organization,
        status=OrganizationMember.MembershipStatus.ACTIVE,
        tier=tier_b,
    )

    ticket_holder: RevelUser = revel_user_factory()
    ticket_tier = TicketTier.objects.create(event=event, name="General")
    Ticket.objects.create(event=event, tier=ticket_tier, user=ticket_holder, status=Ticket.TicketStatus.ACTIVE)

    rsvp_user: RevelUser = revel_user_factory()
    EventRSVP.objects.create(event=event, user=rsvp_user, status=EventRSVP.RsvpStatus.YES)

    invited: RevelUser = revel_user_factory()
    EventInvitation.objects.create(event=event, user=invited)

    return World(
        organization=organization,
        event=event,
        tier_a=tier_a,
        tier_b=tier_b,
        users={
            "anonymous": AnonymousUser(),
            "unrelated": unrelated,
            "owner": organization.owner,
            "org_staff": org_staff,
            "member_no_tier": member_no_tier,
            "member_tier_a": member_tier_a,
            "member_tier_b": member_tier_b,
            "ticket_holder": ticket_holder,
            "rsvp": rsvp_user,
            "invited": invited,
        },
    )


def _make_poll(
    world: World,
    *,
    vote_visibility: ResourceVisibility = ResourceVisibility.PUBLIC,
    result_visibility: ResourceVisibility = ResourceVisibility.PUBLIC,
    result_timing: Poll.PollResultTiming = Poll.PollResultTiming.NEVER,
    status: Poll.PollStatus = Poll.PollStatus.OPEN,
) -> Poll:
    """Create a poll (with its own questionnaire) always attached to the world's event.

    The event attachment is unconditional because the
    ``poll_restricted_visibility_requires_event`` check constraint rejects
    PRIVATE/ATTENDEES_ONLY polls without one.
    """
    return Poll.objects.create(
        organization=world.organization,
        questionnaire=Questionnaire.objects.create(name=f"Parity questionnaire {timezone.now().timestamp()}"),
        event=world.event,
        vote_visibility=vote_visibility,
        result_visibility=result_visibility,
        result_timing=result_timing,
        status=status,
        closed_at=timezone.now() if status == Poll.PollStatus.CLOSED else None,
    )


def _annotate(poll: Poll, user: UserLike) -> Poll:
    """Re-fetch ``poll`` the way the list endpoint does, with the per-user annotations."""
    return Poll.objects.with_user_annotations(user).get(pk=poll.pk)


@pytest.mark.parametrize("visibility", list(ResourceVisibility))
@pytest.mark.parametrize("user_key", USER_STATES)
@pytest.mark.parametrize("tier_restricted", [False, True])
def test_passes_visibility_parity(
    world: World,
    visibility: ResourceVisibility,
    user_key: str,
    tier_restricted: bool,
) -> None:
    """``_passes_visibility`` and ``passes_visibility_from_annotations`` must agree."""
    poll = _make_poll(world, vote_visibility=visibility)
    if tier_restricted:
        poll.vote_membership_tiers.add(world.tier_a)

    user = world.users[user_key]
    tier_ids = list(poll.vote_membership_tiers.values_list("id", flat=True))

    from_instance = eligibility._passes_visibility(user, poll, visibility, poll.vote_membership_tiers.all())
    from_annotations = eligibility.passes_visibility_from_annotations(user, _annotate(poll, user), visibility, tier_ids)

    assert from_annotations == from_instance, (
        f"visibility={visibility} user={user_key} tier_restricted={tier_restricted}: "
        f"instance={from_instance} annotations={from_annotations}"
    )


# The user states that matter for the results gate: one from each side of the audience
# check plus the staff/owner short-circuit.
RESULT_USER_STATES: list[str] = [
    "anonymous",
    "unrelated",
    "owner",
    "org_staff",
    "member_tier_a",
    "member_tier_b",
    "ticket_holder",
]

# (result_visibility, restrict to tier_a) combinations exercising allow, deny and tier refinement.
RESULT_AUDIENCES: list[tuple[ResourceVisibility, bool]] = [
    (ResourceVisibility.PUBLIC, False),
    (ResourceVisibility.MEMBERS_ONLY, True),
    (ResourceVisibility.ATTENDEES_ONLY, False),
]


@pytest.mark.parametrize("result_timing", list(Poll.PollResultTiming))
@pytest.mark.parametrize("has_voted", [False, True])
@pytest.mark.parametrize("is_closed", [False, True])
def test_can_see_results_parity(
    world: World,
    result_timing: Poll.PollResultTiming,
    has_voted: bool,
    is_closed: bool,
) -> None:
    """``can_see_results`` and ``can_see_results_from_annotations`` must agree."""
    status = Poll.PollStatus.CLOSED if is_closed else Poll.PollStatus.OPEN

    for result_visibility, tier_restricted in RESULT_AUDIENCES:
        poll = _make_poll(
            world,
            result_visibility=result_visibility,
            result_timing=result_timing,
            status=status,
        )
        if tier_restricted:
            poll.result_membership_tiers.add(world.tier_a)
        tier_ids = list(poll.result_membership_tiers.values_list("id", flat=True))

        if has_voted:
            for key in RESULT_USER_STATES:
                voter = world.users[key]
                if voter.is_anonymous:
                    continue
                QuestionnaireSubmission.objects.create(
                    user=voter,
                    questionnaire_id=poll.questionnaire_id,
                    status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
                    submitted_at=timezone.now(),
                )

        for key in RESULT_USER_STATES:
            user = world.users[key]
            from_instance = eligibility.can_see_results(user, poll)
            from_annotations = eligibility.can_see_results_from_annotations(user, _annotate(poll, user), tier_ids)
            assert from_annotations == from_instance, (
                f"timing={result_timing} visibility={result_visibility} user={key} "
                f"voted={has_voted} closed={is_closed}: "
                f"instance={from_instance} annotations={from_annotations}"
            )
